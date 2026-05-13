"""pricing.* skills — recommend pricing structure that buyers actually pay.

Today: ``pricing.recommend_tiers`` — given product + audience + competitor
prices, returns a 2-3 tier structure with monthly + annual prices,
features-per-tier, willingness-to-pay reasoning, and an opening promo
offer. Pairs with ``validate.score_idea`` (which scores willingness_to_pay)
and ``landing.draft_copy`` (which needs the prices for the page).
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
Recommend a SaaS pricing structure for the Founder's product.

Product (one-liner): {product}
Target audience: {audience}
Stated value (the dollar/time saving the buyer should expect): {value}
Competitor reference prices: {competitors}
Stage: {stage}  # waitlist | early-paid | scaling

Respond with strict JSON only:
{{
  "tiers": [
    {{
      "name": "<short label, e.g. 'Solo'>",
      "price_monthly_usd": <number>,
      "price_annual_usd": <number>,
      "features": ["<feature>", "<feature>"],
      "fits_who": "<one sentence — which sub-audience this tier targets>"
    }}
  ],
  "opening_promo": "<a launch-week offer that's cheap to honor and credible — e.g. '50% off first 6 months for first 20 customers'>",
  "willingness_to_pay_reasoning": "<one paragraph: why this audience pays this much for this value, anchored against the competitor prices>",
  "what_not_to_do": ["<pricing trap to avoid>", "..."]
}}

Rules:
- 2-3 tiers max. More than 3 = analysis paralysis. If unsure, pick 2.
- Annual price = monthly * 10 (2 months free) by convention. Adjust if the audience expects different.
- Features per tier add value; don't just remove features from higher tiers ("dark patterns").
- Tier names should be descriptive of who buys, not generic ("Solo" beats "Pro"; "Agency" beats "Enterprise").
- The opening_promo must be cheap to honor — no "free forever" traps.
- what_not_to_do calls out pitfalls specific to THIS audience (e.g., "don't price under $X for B2B — looks like a toy").
"""


class RecommendTiers(Skill):
    spec = SkillSpec(
        name="pricing.recommend_tiers",
        description=(
            "Recommend 2-3 pricing tiers (monthly + annual + features + "
            "fits_who) plus a launch-week opening promo, willingness-to-pay "
            "reasoning, and what_not_to_do for this audience."
        ),
        parameters={
            "product": "Product one-liner (what it does, who for)",
            "audience": "Specific buyer description",
            "value": "Stated dollar/time saving the buyer should expect",
            "competitors": "Comma-separated 'name @ $price' references",
            "stage": "waitlist | early-paid | scaling",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        prompt = _PROMPT.format(
            product=str(args.get("product") or "(unspecified)"),
            audience=str(args.get("audience") or "(unspecified)"),
            value=str(args.get("value") or "(unspecified)"),
            competitors=str(args.get("competitors") or "(none provided)"),
            stage=str(args.get("stage") or "early-paid"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the pricing-recommendation skill in Korpha. "
                        "You've seen 1000s of B2B/B2C SaaS launches. Anchor on "
                        "real comp prices, not gut. Be opinionated about what "
                        "NOT to do."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-pricing-{ctx.business.id}",
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
        if parsed is None or not isinstance(parsed.get("tiers"), list):
            raise SkillError(
                f"pricing.recommend_tiers returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        tiers = parsed["tiers"]
        first_tier = tiers[0] if tiers else {}
        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"{len(tiers)} tier(s) | starting "
                f"${first_tier.get('price_monthly_usd', '?')}/mo"
            ),
            payload={
                "tiers": tiers,
                "opening_promo": str(parsed.get("opening_promo") or "").strip(),
                "willingness_to_pay_reasoning": str(
                    parsed.get("willingness_to_pay_reasoning") or ""
                ).strip(),
                "what_not_to_do": list(parsed.get("what_not_to_do") or []),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


register(RecommendTiers())
