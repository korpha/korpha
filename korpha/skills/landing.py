"""landing.* skills — landing-page copy that converts.

Today: ``landing.draft_copy`` — produces headline / subhead / 3-bullet value
prop / CTA copy / objection-handlers, tuned to a specific audience and
single value prop. Used by CTO to populate a Carrd/Framer/Webflow page or
fed into a coding CLI to generate full HTML.
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
You are writing high-conversion landing-page copy for a wannabe-solopreneur's
new offer. Punchy, specific, no marketing fluff.

Audience (who buys): {audience}
Core value prop (one sentence): {value_prop}
Tone: {tone}
Stage: {stage}  # "waitlist" | "live" | "paid"
Primary CTA verb: {cta_verb}  # e.g. "Get early access", "Start free", "Book a call"

Respond with strict JSON only:
{{
  "headline": "<8-12 words, the bold promise>",
  "subhead": "<one sentence: who it's for + the outcome>",
  "value_bullets": [
    "<benefit + proof point>",
    "<benefit + proof point>",
    "<benefit + proof point>"
  ],
  "cta_primary": "<button copy, 3-5 words>",
  "cta_supporting": "<one sentence next to the button — risk reversal or social proof>",
  "objection_handlers": [
    {{"objection": "<expected reservation>", "answer": "<one-sentence response>"}}
  ]
}}

Rules:
- Headline names the specific audience and the specific outcome. Avoid
  abstract verbs ("transform", "revolutionize"). Use concrete nouns.
- value_bullets each pair a benefit with a proof point or quantification.
- objection_handlers: 2-3 of the most likely "but..." reactions. Pre-empt them.
"""


class DraftLandingCopy(Skill):
    spec = SkillSpec(
        name="landing.draft_copy",
        description=(
            "Write high-conversion landing-page copy: headline, subhead, "
            "3 value bullets, primary CTA + supporting line, and "
            "objection handlers. Tuned to audience + value prop + stage."
        ),
        parameters={
            "audience": "Who specifically buys this (avatar)",
            "value_prop": "One sentence — the pain you remove + the outcome",
            "tone": "e.g. 'direct, technical', 'warm, personal'",
            "stage": "waitlist | live | paid",
            "cta_verb": "e.g. 'Get early access', 'Start free trial'",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        prompt = _PROMPT.format(
            audience=str(args.get("audience") or "(unspecified)"),
            value_prop=str(args.get("value_prop") or "(unspecified)"),
            tone=str(args.get("tone") or "direct, founder-led"),
            stage=str(args.get("stage") or "waitlist"),
            cta_verb=str(args.get("cta_verb") or "Get early access"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the landing-page copy skill in Korpha. "
                        "Write copy that a real Founder would put live. "
                        "Avoid generic SaaS-speak; be concrete."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-landing-{ctx.business.id}",
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
        if parsed is None or not parsed.get("headline"):
            raise SkillError(
                f"landing.draft_copy returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        return SkillResult(
            skill_name=self.spec.name,
            summary=str(parsed.get("headline", ""))[:120],
            payload={
                "headline": parsed.get("headline", ""),
                "subhead": parsed.get("subhead", ""),
                "value_bullets": list(parsed.get("value_bullets") or []),
                "cta_primary": parsed.get("cta_primary", ""),
                "cta_supporting": parsed.get("cta_supporting", ""),
                "objection_handlers": list(parsed.get("objection_handlers") or []),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


register(DraftLandingCopy())
