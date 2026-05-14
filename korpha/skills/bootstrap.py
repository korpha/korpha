"""``business.bootstrap_from_brief`` — turn a multi-line founder
brief into real DB state (Lines + Line VPs + seed kanban cards).

Problem this solves: ``CEO.handle_stream()`` is single-skill per
turn. When the Founder says "build me 4 different businesses,"
neither ``hr.start_business_line`` (one line at a time) nor any
existing primitive can fan out within one router call. The CEO
then falls back to a markdown plan and *nothing happens in the DB*.

This skill is the fan-out primitive: it parses the brief with an
LLM, then loops through ``StartBusinessLineSkill`` per line and
seeds 3-5 kanban cards under each spawned unit so the Line VPs
have real backlog to pull from.

Companion to ``hr.start_business_line``. The router picks
``business.bootstrap_from_brief`` when the founder's message is
clearly a multi-line directive ("execute these 10 businesses",
"build me POD + KDP + YouTube channel from this video", etc.).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from korpha.audit.model import InferenceTier
from uuid import UUID

from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import BusinessUnitKind
from korpha.inference.cost_tracker import CompletionRequest
from korpha.inference.types import Message, Role
from korpha.kanban.board import KanbanBoard, CreateCardInput
from korpha.kanban.model import CardPriority
from korpha.skills.business_units import StartBusinessLineSkill
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult,
    SkillSpec,
)


logger = logging.getLogger(__name__)


_VALID_KINDS = {"pod", "kdp", "info", "saas", "affiliate", "agency"}
_MAX_LINES_PER_BRIEF = 6  # guardrail
_CARDS_PER_LINE_MAX = 6


_PLANNER_PROMPT = """\
You are a chief of staff parsing a founder's free-text brief into a
machine-actionable plan to bootstrap N business lines under the
parent business.

Each Line must map to one of these canonical kinds:
- pod (print-on-demand: t-shirts, mugs, posters → Etsy/Shopify/Amazon)
- kdp (Amazon Kindle Direct Publishing: books, journals, workbooks)
- info (info products: courses, ebooks, memberships)
- saas (software-as-a-service web/mobile apps)
- affiliate (review sites, affiliate content, comparison hubs)
- agency (done-for-you services; ONLY if no 1-1 customization required)

RULES:
1. ONLY emit lines that are clearly one-to-many. If a line implies
   any 1-1 customization (custom portraits, Fiverr gigs, lead-gen
   for individual local businesses), DROP IT and note in
   `excluded` why.
2. Use the founder's stated constraints. If the founder said "no 1-1"
   honor it — exclude every 1-1 business idea.
3. Each line must have:
   - kind (one of the canonical kinds above)
   - name (short display, ≤40 chars)
   - rationale (≤180 chars; why this line, what it sells, target)
   - first_cards: 3-5 kanban backlog titles, each a concrete first
     deliverable. NOT epics, NOT "plan X". Real shippable units.
     Examples: "Generate 10 evergreen t-shirt designs (cat × food
     niche)" / "Draft KDP listing copy + categories for first
     learn-to-draw book" / "Set up Printify account + connect Etsy".
4. Max {max_lines} lines. Pick the strongest {max_lines} if the
   brief mentions more. Quality over coverage.
5. If two ideas overlap (e.g. two KDP book types), prefer
   bundling them under one line.

OUTPUT — pure JSON only, no markdown fence, no commentary:
{{
  "lines": [
    {{
      "kind": "<one of: pod | kdp | info | saas | affiliate | agency>",
      "name": "<display name>",
      "rationale": "<one sentence>",
      "first_cards": ["title 1", "title 2", "title 3"]
    }}
  ],
  "excluded": ["<brief idea>: <reason dropped>", ...]
}}

Founder brief:
\"\"\"
{brief}
\"\"\"
"""


def _coerce_kind(raw: Any) -> str | None:
    s = str(raw or "").strip().lower()
    # Tolerate variants the LLM might emit.
    aliases = {
        "print-on-demand": "pod",
        "print_on_demand": "pod",
        "printondemand": "pod",
        "merch": "pod",
        "amazon-kdp": "kdp",
        "publishing": "kdp",
        "course": "info",
        "membership": "info",
        "ebook": "info",
        "app": "saas",
    }
    s = aliases.get(s, s)
    return s if s in _VALID_KINDS else None


def _parse_plan(raw: str) -> dict[str, Any]:
    """Extract the JSON plan from the LLM response. Tolerates
    leading/trailing whitespace and an accidental ```json fence."""
    text = raw.strip()
    if text.startswith("```"):
        # ```json\n...\n```
        text = text.lstrip("`")
        if text.startswith("json"):
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Fallback: find the first { ... last }.
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise SkillError(
            f"business.bootstrap_from_brief: planner LLM emitted "
            f"unparseable JSON. first 300 chars: {raw[:300]!r}"
        ) from exc


class BootstrapFromBriefSkill(Skill):
    """Fan-out skill: brief → N Lines → N×K kanban cards.

    Single-skill compatible — the CEO's per-turn router picks it
    once and inside the run we orchestrate every needed DB write.
    """

    spec = SkillSpec(
        name="business.bootstrap_from_brief",
        description=(
            "USE THIS SKILL when the founder asks to set up, "
            "build, launch, or execute MULTIPLE business lines "
            "in one go. Triggers: any message mentioning 2+ of "
            "{KDP, POD, t-shirts, mugs, Etsy, Printify, "
            "YouTube channel, courses, SaaS, affiliate, agency}; "
            "any 'build me <N> businesses', 'execute these 10 "
            "ideas', 'set up POD + KDP', 'spawn these lines'; "
            "any pasted YouTube transcript listing business "
            "methods. THIS IS THE ONLY SKILL that fans out to "
            "create more than one BusinessUnit per turn — "
            "hr.start_business_line creates exactly one. Pass "
            "the founder's whole message as `brief`. Internally "
            "parses the brief into N line configs, creates the "
            "BusinessUnit + hires the Line VP per line, and "
            "seeds 3-6 starter kanban cards per line."
        ),
        parameters={
            "brief": (
                "The founder's free-text brief, verbatim. Pass "
                "the founder's whole message — do not summarize."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        brief = str(args.get("brief") or "").strip()
        if not brief:
            raise SkillError(
                "business.bootstrap_from_brief: brief is required"
            )
        if len(brief) < 40:
            raise SkillError(
                "business.bootstrap_from_brief: brief is too short to "
                "warrant fan-out; route to hr.start_business_line "
                "instead"
            )

        # 1. Plan: LLM-parse the brief into a structured set of lines.
        planner_msg = _PLANNER_PROMPT.format(
            brief=brief, max_lines=_MAX_LINES_PER_BRIEF,
        )
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=(
                    "You return only valid JSON per the schema in "
                    "the user message. No prose. No markdown."
                )),
                Message(role=Role.USER, content=planner_msg),
            ],
            tier=self.spec.default_tier,
            session_key=f"bootstrap-brief-{ctx.business.id}",
            # WORKHORSE here is often a reasoning model that burns
            # 1-3k tokens on CoT before emitting visible JSON. 6k cap
            # gave us empty content + finish=length on the first
            # live run. 32k buys enough headroom for a 6-line plan.
            max_tokens=32_000,
            timeout_seconds=300,
        )
        logger.info(
            "bootstrap.from_brief: planner call starting "
            "(brief_len=%d)", len(brief),
        )
        response = await ctx.cost_tracker.complete(
            request,
            session=ctx.session,
            business_id=ctx.business.id,
            agent_role_id=ctx.invoking_agent_role_id,
        )
        logger.info(
            "bootstrap.from_brief: planner returned content_len=%d "
            "reasoning_len=%d finish=%s in/out=%d/%d model=%s",
            len(response.content or ""),
            len(response.reasoning or ""),
            response.finish_reason,
            response.input_tokens, response.output_tokens,
            response.model,
        )
        # Some providers (DeepSeek V4, R1-style models) put the JSON
        # under `reasoning` when finish_reason=length AND keep
        # `content` empty. Try reasoning as fallback. If still no
        # parseable JSON, retry once at PRO tier (typically a non-
        # reasoning model that emits content directly).
        raw = response.content or ""
        if not raw.strip() and response.reasoning:
            logger.info(
                "bootstrap.from_brief: content empty, falling back "
                "to reasoning (len=%d)", len(response.reasoning)
            )
            raw = response.reasoning
        if not raw.strip():
            logger.info(
                "bootstrap.from_brief: WORKHORSE returned empty — "
                "retrying at PRO tier"
            )
            request_pro = CompletionRequest(
                messages=request.messages,
                tier=InferenceTier.PRO,
                session_key=f"bootstrap-brief-pro-{ctx.business.id}",
                max_tokens=16_000,
                timeout_seconds=300,
            )
            response = await ctx.cost_tracker.complete(
                request_pro,
                session=ctx.session,
                business_id=ctx.business.id,
                agent_role_id=ctx.invoking_agent_role_id,
            )
            raw = response.content or response.reasoning or ""
            logger.info(
                "bootstrap.from_brief: PRO retry returned "
                "content_len=%d reasoning_len=%d",
                len(response.content or ""),
                len(response.reasoning or ""),
            )
        plan = _parse_plan(raw)
        raw_lines = plan.get("lines") or []
        excluded = plan.get("excluded") or []
        if not isinstance(raw_lines, list) or not raw_lines:
            raise SkillError(
                "business.bootstrap_from_brief: planner returned no "
                "lines. Check the brief is a multi-business directive."
            )

        # 2. Spawn each Line.
        start_line = StartBusinessLineSkill()
        board = KanbanBoard(ctx.session)
        bu_board = BusinessUnitBoard(ctx.session)
        results: list[dict[str, Any]] = []
        spawn_errors: list[str] = []

        for raw in raw_lines[:_MAX_LINES_PER_BRIEF]:
            kind = _coerce_kind(raw.get("kind"))
            name = str(raw.get("name") or "").strip()
            rationale = str(raw.get("rationale") or "").strip()
            first_cards = raw.get("first_cards") or []
            if not kind:
                spawn_errors.append(
                    f"skipped line: invalid kind {raw.get('kind')!r}"
                )
                continue
            if not name:
                spawn_errors.append(f"skipped line: missing name ({kind})")
                continue

            try:
                spawn_res = await start_line.run(
                    ctx=ctx, args={"kind": kind, "name": name},
                )
            except SkillError as exc:
                spawn_errors.append(
                    f"{name} ({kind}): start_business_line failed: {exc}"
                )
                continue
            unit_id_str = spawn_res.payload["unit_id"]
            vp_id_str = spawn_res.payload.get("owner_agent_role_id")

            # 3. Seed kanban cards under this unit.
            try:
                unit = bu_board.get(UUID(unit_id_str))
            except (ValueError, TypeError):
                unit = None
            card_ids: list[str] = []
            for card_title in list(first_cards)[:_CARDS_PER_LINE_MAX]:
                title = str(card_title).strip()
                if not title:
                    continue
                if len(title) > 200:
                    title = title[:197] + "…"
                try:
                    card = board.create(CreateCardInput(
                        business_id=ctx.business.id,
                        title=title,
                        body=(
                            f"Seeded by business.bootstrap_from_brief "
                            f"under Line '{name}' ({kind}). "
                            f"Rationale: {rationale}"
                        ),
                        priority=CardPriority.NORMAL,
                        created_by_agent_role_id=(
                            ctx.invoking_agent_role_id
                        ),
                    ))
                    # PR-INT-29 / dispatcher expects business_unit_id
                    # on cards. Board.create() doesn't accept it
                    # directly so we patch it after.
                    if unit is not None:
                        card.business_unit_id = unit.id
                        ctx.session.add(card)
                        ctx.session.commit()
                        ctx.session.refresh(card)
                    card_ids.append(str(card.id))
                except Exception as exc:  # noqa: BLE001
                    spawn_errors.append(
                        f"{name}: card '{title[:40]}' failed: {exc}"
                    )

            results.append({
                "unit_id": unit_id_str,
                "vp_id": vp_id_str,
                "kind": kind,
                "name": name,
                "rationale": rationale,
                "cards_created": len(card_ids),
                "card_ids": card_ids,
            })

        if not results:
            raise SkillError(
                "business.bootstrap_from_brief: no lines spawned. "
                f"Errors: {spawn_errors[:3]}"
            )

        total_cards = sum(r["cards_created"] for r in results)
        line_list = ", ".join(
            f"{r['name']} ({r['kind']})" for r in results
        )
        summary = (
            f"Spawned {len(results)} business line(s) with "
            f"{total_cards} seed cards: {line_list}"
        )
        if excluded:
            summary += f". Excluded {len(excluded)} 1-1 ideas."

        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={
                "lines": results,
                "excluded": list(excluded),
                "spawn_errors": spawn_errors,
                "total_cards": total_cards,
            },
            cost_usd=float(response.cost_usd or 0.0),
        )


register(BootstrapFromBriefSkill())


__all__ = ["BootstrapFromBriefSkill"]
