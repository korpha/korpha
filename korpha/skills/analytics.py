"""analytics.* skills — KPI discipline + numbers-driven calls.

Today: ``analytics.weekly_review`` — different shape from
``finance.weekly_review`` (which is P&L). This one is about ENGAGEMENT
+ FUNNEL metrics: traffic, signups, activation, retention. Returns the
single metric most likely to break the next ceiling, plus the cheapest
experiment to move it.
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
Produce a weekly engagement-metrics review for the Founder.

Period: {period}
Funnel data (one per line "<metric>: <value>"):
{funnel}
Notable shifts: {shifts}
Active experiments: {experiments}

Respond with strict JSON only:
{{
  "headline": "<one sentence — the funnel story>",
  "bottleneck": "<the single funnel stage most likely capping growth right now>",
  "north_star": "<the metric the Founder should watch this week>",
  "cheapest_experiment": "<one experiment Mike could ship in <8h that would move the bottleneck>",
  "expected_impact": "<concrete change to expect, e.g. '+15% activation in 2 weeks'>",
  "metrics_to_kill": ["<vanity metrics to stop tracking>", "..."]
}}

Rules:
- Bottleneck must be a SPECIFIC stage (acquisition / activation /
  retention / referral / revenue), not a vague "engagement".
- Cheapest experiment fits in the Founder's actual time budget; if it
  doesn't, pick a smaller experiment that touches the same lever.
- metrics_to_kill: vanity metrics (page views without intent, raw
  follower count without engagement) — the Founder probably tracks
  some out of habit. Surface 1-3 to drop.
"""


class WeeklyAnalyticsReview(Skill):
    spec = SkillSpec(
        name="analytics.weekly_review",
        description=(
            "Funnel-focused weekly review: identifies the bottleneck stage, "
            "names the north-star metric for the week, proposes the "
            "cheapest experiment to move the bottleneck, and lists vanity "
            "metrics to stop tracking. Pairs with finance.weekly_review."
        ),
        parameters={
            "period": "Period label (e.g. 'Week of 2026-05-12')",
            "funnel": "One-per-line '<metric>: <value>' (visitors, signups, activated, retained, paid)",
            "shifts": "Free-text notable changes vs prior week",
            "experiments": "Comma-separated active experiments",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        prompt = _PROMPT.format(
            period=str(args.get("period") or "this week"),
            funnel=str(args.get("funnel") or "(no funnel data)"),
            shifts=str(args.get("shifts") or "(none)"),
            experiments=str(args.get("experiments") or "(none)"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the analytics-review skill in Korpha. "
                        "Funnel literacy + opinionated calls. Identify the "
                        "bottleneck, name a cheap experiment, kill vanity "
                        "metrics."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-analytics-{ctx.business.id}",
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
        if parsed is None or "headline" not in parsed:
            raise SkillError(
                f"analytics.weekly_review returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )
        return SkillResult(
            skill_name=self.spec.name,
            summary=str(parsed.get("headline", ""))[:160],
            payload={
                "headline": parsed.get("headline", ""),
                "bottleneck": parsed.get("bottleneck", ""),
                "north_star": parsed.get("north_star", ""),
                "cheapest_experiment": parsed.get("cheapest_experiment", ""),
                "expected_impact": parsed.get("expected_impact", ""),
                "metrics_to_kill": list(parsed.get("metrics_to_kill") or []),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


register(WeeklyAnalyticsReview())
