"""Chief of Staff — internal triage agent.

Aggregates blockers from all agents, dedupes, tries cheap resolutions, groups
by topic, prioritizes, and produces a consolidated digest the CEO uses when
talking to the Founder.

Founder never sees CoS directly. The whole point is to keep Mike's inbox
calm: 80%+ of blockers should never reach him; the rest he sees as ONE
prioritized digest, not N parallel pings.

Triage logic is rule-based today (deterministic, fast, free). It can be
upgraded to LLM-driven prioritization once we have enough real-world data
to know what "good triage" looks like.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session

from korpha.approvals.gate import ApprovalGate
from korpha.approvals.model import ActionClass, AutonomyMode
from korpha.audit.model import Activity, ActorType
from korpha.blockers.model import (
    Blocker,
    BlockerKind,
    BlockerStatus,
    BlockerUrgency,
)
from korpha.blockers.queue import BlockerQueue, _urgency_rank
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import RoleType
from korpha.db._base import as_utc, utcnow
from korpha.inference.limits import agent_max_tokens, agent_timeout


@dataclass(frozen=True)
class DigestItem:
    blocker_id: UUID
    title: str
    detail: str
    kind: BlockerKind
    urgency: BlockerUrgency
    options: list[str]
    cos_recommendation: str | None
    requesting_agent_role_id: UUID
    submitted_at: datetime
    minutes_blocked: int


@dataclass(frozen=True)
class Digest:
    """Consolidated summary CoS hands to CEO. CEO uses this in its message
    to the Founder so the user sees ONE prioritized list, not N agent pings."""

    items: list[DigestItem]
    grouped_by_topic: dict[str, list[DigestItem]]
    auto_resolved_count: int
    """How many blockers CoS resolved without bothering anyone — a "savings"
    metric we can surface in the UI ("CoS handled 12 things this week")."""

    dropped_count: int
    total_open: int

    def headline(self) -> str:
        if not self.items:
            return "No blockers requiring Founder attention."
        if len(self.items) == 1:
            return "1 thing needs your attention:"
        return f"{len(self.items)} things need your attention, in priority order:"

    def render(self) -> str:
        lines: list[str] = [self.headline()]
        for idx, item in enumerate(self.items, 1):
            urgency_tag = item.urgency.value.upper()
            lines.append(
                f"\n{idx}. [{urgency_tag}] {item.title}"
                f"\n   blocked: {item.minutes_blocked} min"
            )
            if item.detail:
                lines.append(f"   detail: {item.detail}")
            if item.options:
                lines.append("   options:")
                for opt in item.options:
                    lines.append(f"     - {opt}")
            if item.cos_recommendation:
                lines.append(f"   CoS recommends: {item.cos_recommendation}")
        return "\n".join(lines)


@dataclass
class ChiefOfStaff:
    """Stateful triage service. One instance per session is fine."""

    session: Session
    queue: BlockerQueue
    hiring: HiringService
    gate: ApprovalGate
    max_digest_items: int = 5
    """Hard cap on items shown to Founder per digest. Anything beyond this is
    grouped into a "+N more" footer — keeps the cognitive load bounded."""

    cost_tracker: Any | None = None
    """Optional CostTracker. When set + ``use_llm_triage=True``, CoS makes ONE
    LLM call per digest to coherently rank and recommend across all open
    blockers. Without it, CoS uses the rule-based recommender."""

    use_llm_triage: bool = False
    """Off by default. Flip to True for smarter recommendations once you have
    enough blocker traffic to justify the per-digest token cost."""

    llm_triage_tier: Any = None
    """InferenceTier (default: WORKHORSE — cheap). Set explicitly if you want
    Pro reasoning at higher cost."""

    cos_cache: dict[UUID, UUID] = field(default_factory=dict)
    """Per-business CoS role id cache."""

    def ensure_role(self, business_id: UUID) -> UUID:
        cached = self.cos_cache.get(business_id)
        if cached is not None:
            return cached
        existing = self.hiring.get_active_role(business_id, RoleType.CHIEF_OF_STAFF)
        if existing is None:
            existing = self.hiring.hire(
                business_id,
                RoleType.CHIEF_OF_STAFF,
                title="Chief of Staff",
                source="auto_internal",
            )
        self.cos_cache[business_id] = existing.id
        return existing.id

    def triage_all(self, business_id: UUID) -> int:
        """Run triage over every untriaged blocker. Returns # processed."""
        cos_id = self.ensure_role(business_id)
        processed = 0
        for blocker in self.queue.list_open(
            business_id,
            statuses=(BlockerStatus.OPEN, BlockerStatus.TRIAGED),
        ):
            self._triage_one(blocker, cos_id)
            processed += 1
        return processed

    async def llm_triage(self, business_id: UUID) -> int:
        """Single LLM call ranks all open blockers and crafts recommendations.

        Cheaper than per-blocker calls (one prompt, one response) AND smarter
        than the rule-based default (sees all blockers together, can reason
        about dependencies and priority across them).

        No-op when ``cost_tracker`` isn't configured. Falls back to the
        rule-based path on parse failure so the Founder never sees a stalled
        digest.
        """
        if self.cost_tracker is None:
            # Can't make LLM calls without inference. Use rule-based path.
            return self.triage_all(business_id)

        cos_id = self.ensure_role(business_id)
        # Run the cheap rule-based pass first so each blocker has at least
        # a placeholder recommendation if the LLM call fails.
        self.triage_all(business_id)

        blockers = [
            b
            for b in self.queue.list_open(
                business_id, statuses=(BlockerStatus.AWAITING_FOUNDER,)
            )
            if b.deduped_into_id is None
        ]
        if not blockers:
            return 0

        from korpha.audit.model import InferenceTier as _Tier
        from korpha.inference.types import (
            CompletionRequest as _Req,
        )
        from korpha.inference.types import (
            Message as _Msg,
        )
        from korpha.inference.types import (
            Role as _Role,
        )

        prompt = _build_llm_triage_prompt(blockers)
        tier = self.llm_triage_tier or _Tier.WORKHORSE
        request = _Req(
            messages=[
                _Msg(
                    role=_Role.SYSTEM,
                    content=(
                        "You are the Chief of Staff in an AI cofounder system. "
                        "Triage the queue: rank by what the Founder should "
                        "decide first, and craft a sharp 1-line recommendation "
                        "for each. Be opinionated."
                    ),
                ),
                _Msg(role=_Role.USER, content=prompt),
            ],
            tier=tier,
            session_key=f"cos-triage-{business_id}",
            max_tokens=agent_max_tokens(),
            timeout_seconds=int(agent_timeout()),
        )
        try:
            response = await self.cost_tracker.complete(
                request,
                session=self.session,
                business_id=business_id,
                agent_role_id=cos_id,
            )
        except Exception:
            return len(blockers)

        from korpha._jsonext import extract_json_dict

        parsed = extract_json_dict(response.content) or {}
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return len(blockers)

        # Apply LLM recommendations back onto each blocker.
        by_id = {str(b.id): b for b in blockers}
        for item in items:
            if not isinstance(item, dict):
                continue
            bid = str(item.get("id", ""))
            target = by_id.get(bid)
            if target is None:
                continue
            rec = str(item.get("recommendation", "")).strip()
            note = str(item.get("note", "")).strip()
            if rec:
                target.cos_recommendation = rec
            if note:
                target.cos_notes = note
            self.queue.update(target)
        return len(blockers)

    async def digest_for_ceo_async(self, business_id: UUID) -> Digest:
        """Async digest builder that opts into LLM triage when configured."""
        if self.use_llm_triage and self.cost_tracker is not None:
            await self.llm_triage(business_id)
        else:
            self.triage_all(business_id)
        return self._build_digest(business_id)

    def digest_for_ceo(self, business_id: UUID) -> Digest:
        """Sync digest builder. Always uses rule-based triage even if
        ``use_llm_triage`` is set — async callers should prefer
        ``digest_for_ceo_async``.
        """
        self.triage_all(business_id)
        return self._build_digest(business_id)

    def _build_digest(self, business_id: UUID) -> Digest:

        all_open = self.queue.list_open(business_id)
        awaiting = [b for b in all_open if b.status == BlockerStatus.AWAITING_FOUNDER]

        # Sort by urgency desc, then oldest-first (longer-blocked = higher priority).
        def sort_key(b: Blocker) -> tuple[int, datetime]:
            submitted = as_utc(b.submitted_at) or utcnow()
            return (-_urgency_rank(b.urgency), submitted)

        awaiting.sort(key=sort_key)
        top_items = awaiting[: self.max_digest_items]

        items = [self._to_digest_item(b) for b in top_items]
        grouped: dict[str, list[DigestItem]] = defaultdict(list)
        for item, blocker in zip(items, top_items, strict=False):
            tag = blocker.topic_tag or "other"
            grouped[tag].append(item)

        auto_resolved = sum(
            1 for b in all_open if b.status == BlockerStatus.RESOLVED_BY_COS
        )
        dropped = sum(1 for b in all_open if b.status == BlockerStatus.DROPPED)

        return Digest(
            items=items,
            grouped_by_topic=dict(grouped),
            auto_resolved_count=auto_resolved,
            dropped_count=dropped,
            total_open=len(awaiting),
        )

    # ---- internal triage logic ----
    pass

    def _triage_one(self, blocker: Blocker, cos_id: UUID) -> None:
        if blocker.status == BlockerStatus.OPEN:
            blocker.status = BlockerStatus.TRIAGED
            blocker.triaged_at = utcnow()

        recommendation = self._recommend(blocker)
        if recommendation is not None:
            blocker.cos_recommendation = recommendation

        topic = self._topic_tag(blocker)
        if topic is not None and not blocker.topic_tag:
            blocker.topic_tag = topic

        # Try cheap resolutions for specific kinds.
        resolution = self._try_cheap_resolve(blocker)
        if resolution is not None:
            blocker.status = BlockerStatus.RESOLVED_BY_COS
            blocker.resolution = resolution
            blocker.resolved_at = utcnow()
            self.queue.update(blocker)
            self._log(
                blocker.business_id,
                cos_id,
                "blocker.cos_resolved",
                {"blocker_id": str(blocker.id), "resolution": resolution},
            )
            return

        # If it's an APPROVAL kind and no Approval is yet linked, create one.
        if blocker.kind == BlockerKind.APPROVAL and blocker.approval_id is None:
            self._convert_to_approval(blocker)

        blocker.status = BlockerStatus.AWAITING_FOUNDER
        blocker.surfaced_at = utcnow()
        self.queue.update(blocker)
        self._log(
            blocker.business_id,
            cos_id,
            "blocker.surfaced_to_ceo",
            {"blocker_id": str(blocker.id)},
        )

    def _recommend(self, blocker: Blocker) -> str | None:
        if blocker.cos_recommendation:
            return blocker.cos_recommendation
        if blocker.options:
            # Default heuristic: recommend the first option (the agent put its
            # preferred path first by convention). This is a placeholder — once
            # we have outcome data we can train a smarter recommender.
            return f"Go with: {blocker.options[0]}"
        if blocker.kind == BlockerKind.CLARIFICATION:
            return "Ask Founder to restate the goal in their own words."
        if blocker.kind == BlockerKind.INFO:
            return "Need this data before proceeding — Founder can paste or point us at the source."
        return None

    def _topic_tag(self, blocker: Blocker) -> str | None:
        """Cheap word-boundary keyword tag. Real grouping comes from LLM later.

        Uses word boundaries so e.g. "overspending" doesn't trigger "spend",
        and "engineering" doesn't trigger "engine" / "code" / etc. via
        substring match.
        """
        text = f"{blocker.title} {blocker.detail}".lower()
        for tag, pattern in _TOPIC_PATTERNS:
            if pattern.search(text):
                return tag
        return None

    def _try_cheap_resolve(self, blocker: Blocker) -> str | None:
        """Attempt resolutions that don't require Founder. Today this is just a
        single rule: PERMISSION blockers within an existing AUTO trust envelope
        are auto-resolved."""
        if blocker.kind == BlockerKind.PERMISSION and blocker.options:
            envelope = self.gate.envelope(
                business_id=blocker.business_id,
                action_class=ActionClass.INTERNAL,
                platform=None,
            )
            if envelope.mode == AutonomyMode.AUTO:
                return f"Auto-resolved by CoS within trust envelope: {blocker.options[0]}"
        return None

    def _convert_to_approval(self, blocker: Blocker) -> None:
        """Create a pending Approval that mirrors the blocker."""
        proposal = self.gate.propose(
            business_id=blocker.business_id,
            agent_role_id=blocker.requesting_agent_role_id,
            action_class=ActionClass.INTERNAL,
            proposal_summary=blocker.title,
            action_payload={
                "from_blocker": str(blocker.id),
                "detail": blocker.detail,
                "options": blocker.options,
            },
        )
        # propose returns ProposalAccepted | ProposalPending | ProposalDenied;
        # we only need the approval_id field if pending.
        approval_id = getattr(proposal, "approval_id", None)
        if approval_id is not None:
            blocker.approval_id = approval_id

    def _to_digest_item(self, b: Blocker) -> DigestItem:
        submitted = as_utc(b.submitted_at) or utcnow()
        minutes_blocked = max(0, int((utcnow() - submitted).total_seconds() // 60))
        return DigestItem(
            blocker_id=b.id,
            title=b.title,
            detail=b.detail,
            kind=b.kind,
            urgency=b.urgency,
            options=list(b.options),
            cos_recommendation=b.cos_recommendation,
            requesting_agent_role_id=b.requesting_agent_role_id,
            submitted_at=submitted,
            minutes_blocked=minutes_blocked,
        )

    def _log(
        self,
        business_id: UUID,
        actor_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.session.add(
            Activity(
                business_id=business_id,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                event_type=event_type,
                payload=payload,
            )
        )
        self.session.commit()


def _build_llm_triage_prompt(blockers: list[Blocker]) -> str:
    """Compact prompt: list every blocker, ask for ranked recommendations."""
    lines = []
    for b in blockers:
        opts = " | ".join(b.options) if b.options else "(no options)"
        lines.append(
            f'- id="{b.id}" urgency={b.urgency.value} kind={b.kind.value} '
            f'topic={b.topic_tag or "?"}\n'
            f'  title: {b.title}\n'
            f'  detail: {b.detail or "(none)"}\n'
            f'  options: {opts}'
        )
    catalog = "\n".join(lines)
    return (
        "Here is the queue of open blockers needing Founder attention. Rank "
        "by what the Founder should decide first (impact x time-blocked x "
        "urgency x dependency on other blockers). For each, write a sharp "
        "1-line recommendation the Founder can act on, plus optional notes.\n\n"
        f"{catalog}\n\n"
        "Respond with strict JSON only:\n"
        "{\n"
        '  "items": [\n'
        '    {"id": "<copy from input>", "rank": <1-N>, '
        '"recommendation": "<one line, opinionated>", '
        '"note": "<optional, 0-2 sentences>"}\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- One entry per blocker. Use the exact ids from the input.\n"
        "- Recommendation references specific options when present; not generic.\n"
        "- If two blockers are about the same topic, recommend whether to "
        "decide them together or sequence them."
    )


# Order matters — first match wins. Engineering before email/social so e.g.
# "deploy the email service" tags as engineering, not email-marketing.
_TOPIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "engineering",
        re.compile(
            r"\b(deploy|deployment|build|repo|branch|code|infrastructure|"
            r"hosting|database|api|ci|cd|merge|pr)\b"
        ),
    ),
    (
        "spend",
        re.compile(
            r"(?:\bbudget\b|\bspend\b|\bcost\b|\bprice\b|\$\d|\bdollars?\b|\bmrr\b)"
        ),
    ),
    (
        "social",
        re.compile(
            r"\b(post|tweet|twitter|linkedin|facebook|instagram|tiktok|threads|social)\b"
        ),
    ),
    (
        "email",
        re.compile(r"\b(email|outreach|newsletter|cold[- ]?email|drip)\b"),
    ),
    (
        "team",
        re.compile(r"\b(hire|hiring|fire|onboard|director|specialist)\b"),
    ),
    (
        "brand",
        re.compile(r"\b(brand|tone|voice|positioning|messaging)\b"),
    ),
]
