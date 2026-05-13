"""growth.* skills — recurring growth ops (content, social, ads).

Today: ``growth.draft_content_plan`` — given audience + channels +
posting cadence, returns a 7-day content plan with channel-tagged posts,
each with a hook, body, and CTA. Used by CMO weekly to keep content
flowing without each-day-a-decision overhead.
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
Plan one week (7 days) of content for the Founder's audience.

Audience: {audience}
Active channels: {channels}
Posting cadence: {cadence}
Brand voice: {voice}
Themes: {themes}

Respond with strict JSON only:
{{
  "posts": [
    {{
      "day": "<Mon|Tue|Wed|Thu|Fri|Sat|Sun>",
      "channel": "<one of the active channels>",
      "format": "<thread|short post|carousel|video|email>",
      "hook": "<scroll-stopping first line, max 12 words>",
      "body": "<2-5 sentence draft>",
      "cta": "<one short action — sign-up, reply, click>"
    }}
  ],
  "experiment": "<one A/B test you'd run this week, plus what 'winning' looks like>",
  "skip_reason_if_any": "<channel|day to deliberately skip + why; empty if none>"
}}

Rules:
- Match cadence to channel — Twitter/X can sustain daily; LinkedIn 3x/week is plenty.
- Hooks should be specific, not "Have you ever wondered". Use numbers, names, sharp claims.
- Don't echo the brand voice into every post in the same way; vary openers and shapes.
- The experiment should be cheap and yield a clear signal in <7 days.
"""


class DraftContentPlan(Skill):
    spec = SkillSpec(
        name="growth.draft_content_plan",
        description=(
            "Plan 7 days of content with channel-tagged posts (hook, body, "
            "CTA per post), one cheap A/B experiment for the week, and an "
            "explicit skip_reason if any day/channel should sit out."
        ),
        parameters={
            "audience": "Who you're posting to (specific avatar)",
            "channels": "Comma-separated active channels (twitter, linkedin, email, etc.)",
            "cadence": "Posts per week / per channel",
            "voice": "Brand voice (e.g. 'direct, technical, founder-led')",
            "themes": "Comma-separated content themes for the week",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        # Cadence sized to the Founder's actual time budget — 30h/week is
        # a different content plan than 5h/week.
        brief = ctx.business.founder_brief or {}
        default_cadence = _cadence_from_brief(brief) or "5 posts/week split"
        prompt = _PROMPT.format(
            audience=str(args.get("audience") or "(unspecified)"),
            channels=str(args.get("channels") or "twitter, linkedin, email"),
            cadence=str(args.get("cadence") or default_cadence),
            voice=str(args.get("voice") or "direct, founder-led"),
            themes=str(args.get("themes") or "(unspecified)"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the content-planning skill in Korpha. "
                        "Write content a real Founder would post — concrete, "
                        "specific, opinionated. No 'hot takes' filler."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-growth-{ctx.business.id}",
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
        if parsed is None or not isinstance(parsed.get("posts"), list):
            raise SkillError(
                f"growth.draft_content_plan returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        posts = parsed["posts"]
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"{len(posts)} posts across the week",
            payload={
                "posts": posts,
                "experiment": str(parsed.get("experiment") or "").strip(),
                "skip_reason_if_any": str(parsed.get("skip_reason_if_any") or "").strip(),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


def _cadence_from_brief(brief: dict[str, Any]) -> str:
    """Pick a sensible posts/week target from the Founder's hours/week.

    Rough heuristic: 1 post per ~2h of available content time. We also
    cap at 7/week (one a day) — past that the content plan is gas, not
    distribution."""
    hours = brief.get("time_per_week_hours")
    if not hours:
        return ""
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return ""
    if h <= 0:
        return ""
    posts = max(1, min(7, h // 2))
    return f"{posts} posts/week split across channels"


register(DraftContentPlan())
