"""finance.* skills — periodic P&L + spend reviews.

Today: ``finance.weekly_review`` — given the week's revenue, costs, and
notable events, produces a one-page review: trend lines, anomalies, top
3 levers to pull next week, and a "would-recommend-cut" list. CEO uses
this in monthly Founder check-ins.
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
Produce a tight weekly P&L + ops review for the Founder.

Period: {period}
Revenue this period (USD): {revenue}
Cost lines (one per line "<name>: $<amount>"):
{costs}
New customers: {new_customers}
Churned customers: {churned}
Notable events: {events}

Respond with strict JSON only:
{{
  "headline": "<one sentence — the story of the week>",
  "trend": "improving | stable | declining",
  "key_metrics": {{
    "revenue_usd": <number>,
    "costs_usd": <number>,
    "net_usd": <number>,
    "new_customers": <int>,
    "churned": <int>,
    "net_customers": <int>
  }},
  "anomalies": [
    "<unusual cost or signal worth pulling on>"
  ],
  "top_levers": [
    "<single lever to pull next week + the expected impact>"
  ],
  "would_recommend_cut": [
    "<expense or activity that's not earning its keep>"
  ]
}}

Rules:
- The headline names the actual movement, not "great progress!". If it's
  flat, say flat.
- top_levers must be concrete and within reach for a small team. No
  vague "improve marketing" — name a specific channel + experiment.
- would_recommend_cut should be empty array if nothing's clearly waste.
"""


class WeeklyReview(Skill):
    spec = SkillSpec(
        name="finance.weekly_review",
        description=(
            "Tight weekly review: headline, trend, key metrics, anomalies, "
            "top levers to pull next week, and what to cut. Used in monthly "
            "Founder check-ins to keep tactical work tied to numbers."
        ),
        parameters={
            "period": "Period label (e.g. 'Week of 2026-05-12')",
            "revenue": "Revenue in USD (number)",
            "costs": "Cost lines — one per line '<name>: $<amount>'",
            "new_customers": "Count this period (int)",
            "churned": "Count this period (int)",
            "events": "Free-text notable events (launches, outages, press)",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        prompt = _PROMPT.format(
            period=str(args.get("period") or "this week"),
            revenue=str(args.get("revenue") or "0"),
            costs=str(args.get("costs") or "(no costs)"),
            new_customers=str(args.get("new_customers") or "0"),
            churned=str(args.get("churned") or "0"),
            events=str(args.get("events") or "(none)"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the finance-review skill in Korpha. Honest "
                        "numbers, opinionated levers. No cheerleading."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-finance-{ctx.business.id}",
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
                f"finance.weekly_review returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        return SkillResult(
            skill_name=self.spec.name,
            summary=str(parsed.get("headline", ""))[:160],
            payload={
                "headline": parsed.get("headline", ""),
                "trend": parsed.get("trend", "stable"),
                "key_metrics": parsed.get("key_metrics") or {},
                "anomalies": list(parsed.get("anomalies") or []),
                "top_levers": list(parsed.get("top_levers") or []),
                "would_recommend_cut": list(parsed.get("would_recommend_cut") or []),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


_MONTHLY_PROMPT = """\
Produce a structured monthly P&L + strategy review.

Period: {period} ({days} days)

Revenue this period (USD): {revenue}
  - Recorded transactions: {revenue_count}

Spend this period (USD): {spend}
  - Top cost categories (provider × tier):
{spend_breakdown}

Activity:
  - Cards shipped to DONE: {shipped}
  - Cards in REVIEW awaiting verification: {review}
  - Cards still in IN_PROGRESS at month-end: {in_progress}
  - Open blockers at month-end: {blockers}

Notable events: {events}

Respond with strict JSON only:
{{
  "headline": "<one sentence — the month in a single line>",
  "trend": "improving | stable | declining",
  "month_metrics": {{
    "revenue_usd": <number>,
    "spend_usd": <number>,
    "net_usd": <number>,
    "shipped_cards": <int>,
    "spend_per_shipped": <number>
  }},
  "wins": [
    "<concrete thing that landed this month + why it matters>"
  ],
  "concerns": [
    "<thing that's slipping or unhealthy>"
  ],
  "strategy_proposal": {{
    "next_month_focus": "<one sentence — what to push on next>",
    "tasks": [
      "[CTO|CMO|COO] <concrete next-month action>"
    ],
    "kpi_target": "<what number we'd watch + the goal>"
  }}
}}

Rules:
- Headline tells the truth (flat = flat, regression = regression).
- Wins / concerns must reference actual cards, costs, or events
  from the data above — no generic platitudes.
- The strategy_proposal.tasks list must be 1-4 entries, each
  prefixed with the C-suite role tag so the workforce can
  dispatch them as kanban cards.
- KPI target: a number + verb (e.g. "ship 4 cards in Aug",
  "land first 5 paying customers", "hold spend under $30").
"""


class MonthlyReview(Skill):
    """Pulls last 30 days from the DB + asks the LLM for a
    structured monthly P&L review with a strategy proposal."""

    spec = SkillSpec(
        name="finance.monthly_review",
        description=(
            "Generate a monthly P&L + strategy review. Auto-pulls "
            "the last 30 days of Cost (spend), Approval/commerce "
            "events (revenue), and KanbanCard activity (work "
            "shipped vs stuck) from the live DB, then asks the "
            "Pro tier to produce the report + a 1-4 item strategy "
            "proposal. Use at month boundaries to set next month's "
            "focus."
        ),
        parameters={
            "period_label": (
                "Optional. Display label like 'August 2026'. "
                "Defaults to the most recent 30-day window."
            ),
            "events": (
                "Optional. Free-text notable events for the "
                "month (launches, press, outages, hires). Empty "
                "is fine — the report still covers numbers."
            ),
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal

        from sqlmodel import select as _select

        from korpha.approvals.model import Approval, ApprovalStatus
        from korpha.audit.model import Cost
        from korpha.blockers.model import Blocker, BlockerStatus
        from korpha.kanban.model import KanbanCard, KanbanColumn

        now = datetime.now(tz=timezone.utc)
        days = 30
        window_start = now - timedelta(days=days)
        period_label = (
            str(args.get("period_label") or "")
            or now.strftime("%B %Y")
        )

        biz_id = ctx.business.id
        session = ctx.session

        # Spend
        costs = list(session.exec(
            _select(Cost)
            .where(Cost.business_id == biz_id)
            .where(Cost.created_at >= window_start)
        ).all())
        spend_total = sum((c.cost_usd for c in costs), Decimal("0"))
        spend_buckets: dict[tuple[str, str], Decimal] = {}
        for c in costs:
            key = (c.provider, c.tier.value)
            spend_buckets[key] = (
                spend_buckets.get(key, Decimal("0")) + c.cost_usd
            )
        spend_breakdown = "\n".join(
            f"    {provider} × {tier}: ${float(amt):.4f}"
            for (provider, tier), amt in sorted(
                spend_buckets.items(), key=lambda kv: -kv[1],
            )
        ) or "    (no costs in window)"

        # Revenue: prefer real webhook-tracked RevenueEvent rows.
        # Falls back to the legacy approval-based proxy when no
        # webhook events are present in the window (e.g. Stripe
        # webhook isn't configured yet) so installs without
        # webhook setup still get *some* revenue signal.
        from korpha.commerce.revenue import (
            RevenueEvent, RevenueKind, RevenueService,
        )
        from datetime import datetime as _dt
        revenue = Decimal("0")
        revenue_count = 0
        webhook_rows = list(session.exec(
            _select(RevenueEvent)
            .where(RevenueEvent.business_id == biz_id)
            .where(RevenueEvent.occurred_at >= window_start)
        ).all())
        if webhook_rows:
            for r in webhook_rows:
                if r.kind == RevenueKind.REFUND:
                    revenue -= r.amount_usd
                else:
                    revenue += r.amount_usd
            revenue_count = len(webhook_rows)
        else:
            # Legacy proxy: approved create_payment_link approvals.
            approvals = list(session.exec(
                _select(Approval)
                .where(Approval.business_id == biz_id)
                .where(Approval.status == ApprovalStatus.APPROVED)
                .where(Approval.created_at >= window_start)
            ).all())
            for a in approvals:
                payload = a.action_payload or {}
                if payload.get("kind") != "create_payment_link":
                    continue
                amount = payload.get("amount_usd")
                if isinstance(amount, (int, float)):
                    revenue += Decimal(str(amount))
                    revenue_count += 1

        # Activity
        shipped = len(list(session.exec(
            _select(KanbanCard)
            .where(KanbanCard.business_id == biz_id)
            .where(KanbanCard.column == KanbanColumn.DONE)
            .where(KanbanCard.moved_at >= window_start)
        ).all()))
        review_count = len(list(session.exec(
            _select(KanbanCard)
            .where(KanbanCard.business_id == biz_id)
            .where(KanbanCard.column == KanbanColumn.REVIEW)
        ).all()))
        in_progress = len(list(session.exec(
            _select(KanbanCard)
            .where(KanbanCard.business_id == biz_id)
            .where(KanbanCard.column == KanbanColumn.IN_PROGRESS)
        ).all()))
        blockers = len(list(session.exec(
            _select(Blocker)
            .where(Blocker.business_id == biz_id)
            .where(Blocker.status == BlockerStatus.OPEN)
        ).all()))

        prompt = _MONTHLY_PROMPT.format(
            period=period_label,
            days=days,
            revenue=f"${float(revenue):.2f}",
            revenue_count=revenue_count,
            spend=f"${float(spend_total):.2f}",
            spend_breakdown=spend_breakdown,
            shipped=shipped,
            review=review_count,
            in_progress=in_progress,
            blockers=blockers,
            events=str(args.get("events") or "(none)"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the monthly P&L review skill in "
                        "Korpha. Truthful numbers, opinionated "
                        "strategy. No cheerleading; no doom either. "
                        "Concrete next-month proposal."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-finance-monthly-{biz_id}",
            max_tokens=agent_max_tokens(),
            timeout_seconds=agent_timeout(),
        )
        response = await ctx.cost_tracker.complete(
            request, session=session, business_id=biz_id,
            agent_role_id=ctx.invoking_agent_role_id,
        )

        parsed = extract_json_dict(response.content)
        if parsed is None or "headline" not in parsed:
            raise SkillError(
                f"finance.monthly_review returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        return SkillResult(
            skill_name=self.spec.name,
            summary=str(parsed.get("headline", ""))[:160],
            payload={
                "period_label": period_label,
                "headline": parsed.get("headline", ""),
                "trend": parsed.get("trend", "stable"),
                "month_metrics": parsed.get("month_metrics") or {},
                "wins": list(parsed.get("wins") or []),
                "concerns": list(parsed.get("concerns") or []),
                "strategy_proposal": parsed.get("strategy_proposal") or {},
                # Raw inputs the model saw, for traceability.
                "raw_inputs": {
                    "revenue_usd": float(revenue),
                    "spend_usd": float(spend_total),
                    "shipped": shipped,
                    "review": review_count,
                    "in_progress": in_progress,
                    "blockers": blockers,
                },
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


register(WeeklyReview())
register(MonthlyReview())
