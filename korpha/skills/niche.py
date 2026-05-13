"""niche.* skills — micro-niche discovery for solopreneurs.

Today: ``niche.find_micro_niches`` — given the Founder's skills, time budget,
and savings, propose 3-5 specific micro-niche product ideas with rationale,
target avatar, validation cost, and a single experiment to run this week.
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

_NICHE_PROMPT = """\
You are running niche discovery for a wannabe-solopreneur Founder. Output 3-5
specific micro-niches that fit their constraints.

Founder skills: {skills}
Weekly time budget: {time_budget_hours} hours
Cash savings (USD): {savings_usd}
Founder's stated goal: {goal}

For each niche, give:
- name: 3-7 word label (e.g. "Deployment automation for solo Python devs")
- target_avatar: one sentence on who buys it
- value_prop: one sentence on the pain it removes
- price_band: monthly subscription range you'd test (e.g. "$29-99/mo")
- competition: 1 sentence — who's already there, why this slice still has room
- validation_experiment: ONE concrete thing the Founder runs this week
  (recruit X interviews, ship a 1-page landing, post in Y community...) within
  their time + cash budget
- fit_score: 1-10, how well it matches the Founder's skills + budget

Respond with strict JSON only:
{{
  "candidates": [
    {{"name": "...", "target_avatar": "...", "value_prop": "...",
      "price_band": "...", "competition": "...",
      "validation_experiment": "...", "fit_score": 8}}
  ],
  "recommended_index": 0,
  "rationale": "<one sentence why the recommended niche wins>"
}}

Rules:
- Be SPECIFIC. "AI tools for marketers" is worthless. "Email-warmup agent
  for cold-outreach SaaS founders shipping their first 100 sends" is gold.
- Match the price band to who would pay (B2B prosumer = $29-99, B2C = $5-15,
  B2B SaaS = $99-499, agency = $500-2k).
- The validation_experiment must fit the Founder's actual time + cash.
"""


class FindMicroNichesSkill(Skill):
    spec = SkillSpec(
        name="niche.find_micro_niches",
        description=(
            "Given Founder skills + time budget + savings, propose 3-5 "
            "micro-niches with target avatar, value prop, price band, "
            "competition note, and one validation experiment to run this week."
        ),
        parameters={
            "skills": "Founder's skills / strengths (free text)",
            "time_budget_hours": "Hours per week available (number)",
            "savings_usd": "Cash savings (number)",
            "goal": "Stated goal e.g. '$5k MRR side income in 6 months'",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        # Default missing args from the Day-0 founder_brief so a Founder
        # who ran `korpha onboard` doesn't have to repeat themselves.
        brief = ctx.business.founder_brief or {}
        prompt = _NICHE_PROMPT.format(
            skills=str(args.get("skills") or brief.get("skills") or "(unspecified)"),
            time_budget_hours=str(
                args.get("time_budget_hours") or brief.get("time_per_week_hours") or "5"
            ),
            savings_usd=str(
                args.get("savings_usd") or brief.get("savings_usd") or "1000"
            ),
            goal=str(args.get("goal") or brief.get("goal") or "side income"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the niche-discovery skill in Korpha, an AI "
                        "cofounder for solo entrepreneurs. Be ruthless about "
                        "specificity — vague niches help nobody."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-niche-{ctx.business.id}",
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
        if parsed is None or not isinstance(parsed.get("candidates"), list):
            raise SkillError(
                f"niche.find_micro_niches returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        candidates = parsed["candidates"]
        recommended = parsed.get("recommended_index", 0)
        rationale = str(parsed.get("rationale") or "").strip()

        try:
            rec_idx = int(recommended)
        except (TypeError, ValueError):
            rec_idx = 0
        rec_idx = max(0, min(rec_idx, len(candidates) - 1))
        recommended_name = (
            candidates[rec_idx].get("name", "?") if candidates else "(no candidates)"
        )

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Recommended: {recommended_name}",
            payload={
                "candidates": candidates,
                "recommended_index": rec_idx,
                "rationale": rationale,
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


# Self-register with the default registry.
register(FindMicroNichesSkill())
