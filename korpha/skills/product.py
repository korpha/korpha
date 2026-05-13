"""product.* skills — what to actually build first.

Today: ``product.first_feature`` — given a niche + audience + value-prop,
returns 3 candidate v1 features ranked by buy-trigger strength: the
specific moment in the buyer's day that would make them open their wallet.
The ranked recommendation includes a "smallest shippable unit" that fits
the Founder's time + cash budget.
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
Pick the v1 feature for the Founder's product. Rank by *buy-trigger
strength*: the specific moment in the buyer's day that would make them open
their wallet. Vague "comprehensive dashboard" features lose. Sharp
"automates this 2-hour task" wins.

Niche: {niche}
Target audience: {audience}
Value prop: {value_prop}
Founder constraints: {constraints}

Respond with strict JSON only:
{{
  "candidates": [
    {{
      "name": "<3-7 word feature name, action-shaped>",
      "buy_trigger": "<the exact pain moment that triggers a purchase>",
      "smallest_shippable_unit": "<v1 scope cheap enough to ship in days>",
      "build_hours": <number>,
      "trigger_strength": <1-10>,
      "why": "<one sentence on why buyers care>"
    }}
  ],
  "recommended_index": <int>,
  "rationale": "<one paragraph why the recommended one beats the others>",
  "do_not_build": ["<feature you might be tempted to build but shouldn't>", "..."]
}}

Rules:
- 3 candidates. More than 3 = scope creep, you're padding.
- The smallest_shippable_unit must fit the Founder's actual budget;
  if it doesn't, you've picked a too-big feature — go smaller.
- "buy_trigger" must name a specific moment, not a generic concept.
  GOOD: "When their Stripe MRR drops 5% week-over-week."
  BAD:  "When they want to grow their business."
"""


class FirstFeature(Skill):
    spec = SkillSpec(
        name="product.first_feature",
        description=(
            "Pick the v1 feature for the Founder's product. Returns 3 "
            "candidates ranked by buy-trigger strength, each with a "
            "smallest shippable unit, build hours, and explicit "
            "do-not-build list. Used after niche selection, before "
            "writing landing copy."
        ),
        parameters={
            "niche": "The niche (e.g. 'Stripe MRR alerts for indie SaaS')",
            "audience": "Specific buyer description",
            "value_prop": "One-line value (the dollar/time saving)",
            "constraints": "Founder time + cash budget",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        # Default constraints from founder_brief so the recommendation
        # actually fits this Founder, not a generic "5h/week, $500".
        brief = ctx.business.founder_brief or {}
        default_constraints = _brief_constraints(brief) or "5h/week, $500"
        prompt = _PROMPT.format(
            niche=str(args.get("niche") or "(unspecified)"),
            audience=str(args.get("audience") or "(unspecified)"),
            value_prop=str(args.get("value_prop") or "(unspecified)"),
            constraints=str(args.get("constraints") or default_constraints),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the v1-feature-picking skill in Korpha. "
                        "You've shipped enough MVPs to know that scope kills. "
                        "Be ruthless about smallest shippable unit."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-product-{ctx.business.id}",
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
                f"product.first_feature returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )
        candidates = parsed["candidates"]
        try:
            rec_idx = int(parsed.get("recommended_index", 0))
        except (TypeError, ValueError):
            rec_idx = 0
        rec_idx = max(0, min(rec_idx, max(len(candidates) - 1, 0)))
        rec_name = candidates[rec_idx].get("name", "?") if candidates else "(none)"
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"v1 feature: {rec_name}",
            payload={
                "candidates": candidates,
                "recommended_index": rec_idx,
                "rationale": str(parsed.get("rationale") or "").strip(),
                "do_not_build": list(parsed.get("do_not_build") or []),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


def _brief_constraints(brief: dict[str, Any]) -> str:
    """Render founder constraints from the captured brief."""
    parts: list[str] = []
    if brief.get("time_per_week_hours"):
        parts.append(f"{brief['time_per_week_hours']}h/week")
    if brief.get("savings_usd"):
        parts.append(f"${brief['savings_usd']} cash")
    extra = brief.get("constraints") or []
    if isinstance(extra, list):
        parts.extend(str(c) for c in extra if str(c).strip())
    return ", ".join(parts)


register(FirstFeature())
