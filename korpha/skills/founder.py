"""founder.* skills — Day-0 intake.

The single open question from BRIEF.md's 5-minute demo is *"What do you
actually want?"* This skill takes the freeform answer and structures it
into the fields downstream skills need (niche, validate, pricing) so the
Founder doesn't have to repeat themselves on every turn.

The skill writes the structured brief to ``Business.founder_brief``.
Other skills can read it to default their parameters when the Founder
doesn't supply them explicitly.
"""
from __future__ import annotations

from typing import Any

from korpha._jsonext import extract_json_dict
from korpha.audit.model import InferenceTier
from korpha.inference.limits import agent_max_tokens, agent_timeout
from korpha.inference.types import CompletionRequest, Message, Role
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)

_INTAKE_PROMPT = """\
You are the Day-0 intake step in Korpha — an AI cofounder for solo
entrepreneurs. The Founder just answered "What do you actually want?".
Your job: extract the concrete fields downstream skills need so we never
have to ask again.

Founder's answer:
\"\"\"
{answer}
\"\"\"

Extract:
- goal: 1 short line (e.g. "$5k MRR side income in 6 months").
- timeline_months: integer best guess. Default 6 if not stated.
- time_per_week_hours: integer hours/week the Founder has. If they said
  "evenings and weekends" guess 10, "few hours a week" guess 5, "all in"
  guess 30. Default 5 if completely missing.
- savings_usd: integer USD they can spend. Default 1000 if missing.
- skills: short comma list (e.g. "Python, B2B SaaS, indie marketing").
- niches_considered: short list of niches/ideas they mentioned (may be empty).
- constraints: list of hard "NOTs" — won't do crypto, can't quit job, etc.
- summary: one paragraph (≤80 words) restating their plan in YOUR voice
  as their cofounder. Sound like a partner, not a chatbot. End with the
  single most important next action.

Respond with strict JSON only:
{{
  "goal": "...",
  "timeline_months": 6,
  "time_per_week_hours": 10,
  "savings_usd": 1000,
  "skills": "...",
  "niches_considered": ["..."],
  "constraints": ["..."],
  "summary": "..."
}}

Rules:
- Pick best defaults rather than asking back. The Founder hates surveys.
- "summary" must be in second person ("you said you want…") and propose
  ONE clear next action, not a menu.
- If the answer is one line of nonsense, still produce a brief — guess.
"""


class IntakeBriefSkill(Skill):
    spec = SkillSpec(
        name="founder.intake_brief",
        description=(
            "Day-0 intake. Take the Founder's freeform answer to "
            "'what do you actually want?' and structure it into goal, "
            "timeline, time/week, savings, skills, constraints, niches "
            "considered. Saved to Business.founder_brief."
        ),
        parameters={
            "answer": (
                "Founder's freeform answer to 'what do you actually want?'. "
                "Required."
            ),
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        answer = str(args.get("answer") or "").strip()
        if not answer:
            raise SkillError(
                "founder.intake_brief requires `answer` — the Founder's "
                "freeform reply to 'what do you actually want?'"
            )

        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the intake step of Korpha. Be decisive, "
                        "make sensible defaults, never bounce questions back."
                    ),
                ),
                Message(role=Role.USER, content=_INTAKE_PROMPT.format(answer=answer)),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-intake-{ctx.business.id}",
            max_tokens=agent_max_tokens(),
            timeout_seconds=agent_timeout(),
        )
        response = await ctx.cost_tracker.complete(
            request,
            session=ctx.session,
            business_id=ctx.business.id,
            agent_role_id=ctx.invoking_agent_role_id,
        )

        parsed = extract_json_dict(response.content)
        if parsed is None or not isinstance(parsed.get("summary"), str):
            raise SkillError(
                "founder.intake_brief returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        brief = {
            "goal": str(parsed.get("goal") or "").strip(),
            "timeline_months": _coerce_int(parsed.get("timeline_months"), 6),
            "time_per_week_hours": _coerce_int(parsed.get("time_per_week_hours"), 5),
            "savings_usd": _coerce_int(parsed.get("savings_usd"), 1000),
            "skills": str(parsed.get("skills") or "").strip(),
            "niches_considered": _as_str_list(parsed.get("niches_considered")),
            "constraints": _as_str_list(parsed.get("constraints")),
            "summary": str(parsed.get("summary") or "").strip(),
            "raw_answer": answer,
        }

        ctx.business.founder_brief = brief
        ctx.session.add(ctx.business)
        ctx.session.commit()

        goal = brief["goal"] or "(no explicit goal)"
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Captured: {goal}",
            payload=brief,
            cost_usd=0.0,
            raw_response=response.content,
        )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


register(IntakeBriefSkill())


__all__ = ["IntakeBriefSkill"]
