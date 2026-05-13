"""outreach.* skills — drafts personalized cold opens for the Founder.

Today: ``outreach.draft_cold_emails`` — given a target avatar, value prop,
and source channel (LinkedIn, email, Reddit DM, Twitter DM), returns 3
distinct opener variants (different angles) plus a one-line per-prospect
personalization template.
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
You are drafting cold outreach for a wannabe-solopreneur Founder. Goal: get
a 15-minute discovery call, NOT to pitch the product. Treat this like a
warm peer reaching out, not a vendor.

Target avatar: {avatar}
Value hypothesis: {value_prop}
Channel: {channel}  # email | linkedin | twitter_dm | reddit_dm
Founder name: {founder_name}
Founder one-liner: {founder_bio}
Asking for: 15-minute call about their experience with the problem

Respond with strict JSON only:
{{
  "variants": [
    {{
      "angle": "<short label, e.g. 'curiosity', 'shared-problem', 'borrowed-credibility'>",
      "subject": "<channel-appropriate subject or first line>",
      "body": "<3-5 sentence opener. Plain, conversational, ends with a soft ask>"
    }}
  ],
  "personalization_template": "<one-line cue the Founder fills in per prospect, e.g. '{{prospect_recent_post_or_repo}}'>",
  "follow_up_subject": "<for a 1-week-later nudge>"
}}

Rules:
- Three variants, three different ANGLES. Don't reuse the same opener.
- No pitching. The first message should NOT mention a product.
- Soft ask ("would you be open to a 15-minute call?", not "let's schedule").
- LinkedIn variants under 300 chars. Email variants under 130 words.
- Use the Founder's name + one-liner credibly; don't fabricate authority.
"""


class DraftColdEmails(Skill):
    spec = SkillSpec(
        name="outreach.draft_cold_emails",
        description=(
            "Draft 3 distinct cold-outreach variants targeting a specific "
            "avatar to book a 15-minute discovery call. Returns variants, "
            "personalization template, and follow-up subject."
        ),
        parameters={
            "avatar": "Specific target description (role, company size, situation)",
            "value_prop": "What pain you suspect they have",
            "channel": "email | linkedin | twitter_dm | reddit_dm",
            "founder_name": "Founder's first name (for signature)",
            "founder_bio": "One-liner credibility (e.g. 'Python dev, ex-Stripe')",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        # Bio defaults to the brief's skills field — beats generic
        # "indie developer" as a credibility marker in cold outreach.
        brief = ctx.business.founder_brief or {}
        default_bio = str(brief.get("skills") or "").strip() or "indie developer"
        prompt = _PROMPT.format(
            avatar=str(args.get("avatar") or "(unspecified)"),
            value_prop=str(args.get("value_prop") or "(unspecified)"),
            channel=str(args.get("channel") or "email"),
            founder_name=str(args.get("founder_name") or ctx.founder.display_name or "Founder"),
            founder_bio=str(args.get("founder_bio") or default_bio),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the outreach-drafting skill in Korpha. "
                        "Write cold outreach the way a thoughtful peer would, "
                        "not the way a sales bot would. Be specific, brief, "
                        "and end with a soft ask."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-outreach-{ctx.business.id}",
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
        if parsed is None or not isinstance(parsed.get("variants"), list):
            raise SkillError(
                f"outreach.draft_cold_emails returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        variants = parsed["variants"]
        if not variants:
            raise SkillError("outreach.draft_cold_emails returned no variants.")

        first_subject = str(variants[0].get("subject", "") or "")[:80]
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"{len(variants)} variants. First subject: {first_subject}",
            payload={
                "variants": variants,
                "personalization_template": parsed.get("personalization_template", ""),
                "follow_up_subject": parsed.get("follow_up_subject", ""),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


register(DraftColdEmails())
