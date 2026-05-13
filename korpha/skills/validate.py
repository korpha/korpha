"""validate.* skills — sanity-check Founder ideas / niches.

Today: ``validate.score_idea`` — given a niche idea, returns a 1-10 score
across four dimensions (demand signal, willingness-to-pay, founder fit,
distribution path) plus the cheapest test that would fail-fast if the idea
is bad. The CEO uses this to push back on bad ideas with a concrete reason
and a better path — never a dead-end "no".
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

_PROMPT = """\
You are evaluating a niche idea for a wannabe-solopreneur Founder. Be
critical but constructive — the cofounder doesn't dead-end with "no", they
push back with a better path.

Idea: {idea}
Target avatar (if specified): {avatar}
Founder skills: {skills}
Founder constraints: {constraints}

Score on 1-10 (10 = clearly strong) across these dimensions:
- demand_signal: is there evidence people are looking for this?
- willingness_to_pay: do these buyers actually pay for this kind of tool?
- founder_fit: does the Founder have what it takes to win this niche?
- distribution_path: is there a clear, cheap way to reach the buyers?

Respond with strict JSON only:
{{
  "scores": {{
    "demand_signal": <1-10>,
    "willingness_to_pay": <1-10>,
    "founder_fit": <1-10>,
    "distribution_path": <1-10>
  }},
  "overall": <1-10, your gestalt rating, not just an average>,
  "verdict": "go" | "improve" | "kill",
  "strengths": ["<short bullet>", "..."],
  "concerns": ["<short bullet>", "..."],
  "kill_test": "<the cheapest experiment whose negative result would
    decisively kill this idea — within the Founder's time/money budget>",
  "improvement_path": "<if improve: one specific pivot that would lift the
    weakest score>"
}}

Rules:
- "kill" verdicts must come with a kill_test that is specific + cheap.
- "improve" verdicts must come with an improvement_path that is specific.
- Be honest. A 4 is a 4, not a polite 7.
"""


class ScoreIdea(Skill):
    spec = SkillSpec(
        name="validate.score_idea",
        description=(
            "Score a niche idea on demand signal, willingness-to-pay, "
            "founder fit, and distribution path. Returns scores, verdict "
            "(go / improve / kill), strengths, concerns, kill_test, and "
            "improvement_path. Used by CEO to push back constructively."
        ),
        parameters={
            "idea": "The niche idea (one paragraph)",
            "avatar": "Target buyer (optional)",
            "skills": "Founder's skills (optional)",
            "constraints": "Time + money budget (optional)",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        # Default from founder_brief so post-onboarding runs are one-click.
        brief = ctx.business.founder_brief or {}
        default_constraints = _summarize_constraints_from_brief(brief)
        prompt = _PROMPT.format(
            idea=str(args.get("idea") or "(missing)"),
            avatar=str(args.get("avatar") or "(unspecified)"),
            skills=str(args.get("skills") or brief.get("skills") or "(unspecified)"),
            constraints=str(args.get("constraints") or default_constraints or "(unspecified)"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the idea-scoring skill in Korpha. Be "
                        "critical and specific. A bad idea deserves a "
                        "concrete kill_test, not a polite vague pass."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-validate-{ctx.business.id}",
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
        if parsed is None or "scores" not in parsed or "verdict" not in parsed:
            raise SkillError(
                f"validate.score_idea returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        verdict = str(parsed.get("verdict", "")).strip().lower()
        if verdict not in ("go", "improve", "kill"):
            verdict = "improve"

        try:
            overall = int(parsed.get("overall", 0))
        except (TypeError, ValueError):
            overall = 0
        overall = max(0, min(overall, 10))

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"verdict={verdict} | overall={overall}/10",
            payload={
                "scores": parsed.get("scores") or {},
                "overall": overall,
                "verdict": verdict,
                "strengths": list(parsed.get("strengths") or []),
                "concerns": list(parsed.get("concerns") or []),
                "kill_test": parsed.get("kill_test", ""),
                "improvement_path": parsed.get("improvement_path", ""),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


def _summarize_constraints_from_brief(brief: dict[str, Any]) -> str:
    """Build a one-line constraints string from the captured brief.

    Result looks like ``"5h/week, $1000 cash, can't quit job"``. Used as
    the default for skills that ask about Founder constraints when the
    caller doesn't supply them explicitly.
    """
    parts: list[str] = []
    if brief.get("time_per_week_hours"):
        parts.append(f"{brief['time_per_week_hours']}h/week")
    if brief.get("savings_usd"):
        parts.append(f"${brief['savings_usd']} cash")
    extra = brief.get("constraints") or []
    if isinstance(extra, list):
        parts.extend(str(c) for c in extra if str(c).strip())
    return ", ".join(parts)


register(ScoreIdea())
