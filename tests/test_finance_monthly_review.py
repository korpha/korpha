"""Tests for ``finance.monthly_review`` — auto-pulls last 30 days
from the DB and asks the LLM for a structured monthly P&L review."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session

from korpha.approvals.model import (
    ActionClass, Approval, ApprovalStatus,
)
from korpha.audit.model import Cost, InferenceTier
from korpha.business.model import Business
from korpha.cofounder.model import AgentRole
from korpha.identity.model import Founder
from korpha.kanban.model import KanbanCard, KanbanColumn
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


_VALID_REPORT = (
    '{"headline":"Spend held flat, shipped 2 cards, no revenue yet.",'
    '"trend":"stable",'
    '"month_metrics":{"revenue_usd":0,"spend_usd":0.45,"net_usd":-0.45,'
    '"shipped_cards":2,"spend_per_shipped":0.225},'
    '"wins":["Pricing page deployed"],'
    '"concerns":["No paying customers yet"],'
    '"strategy_proposal":{'
    '"next_month_focus":"Drive 10 conversations with the ideal customer profile",'
    '"tasks":["[CMO] post 3 LinkedIn case studies","[COO] schedule 5 founder interviews"],'
    '"kpi_target":"5 ICP interviews booked in next 30 days"}}'
)


class _StubPool:
    def __init__(self, response: str) -> None:
        self._text = response
        self.calls = 0

    async def complete(self, request, *, account=None):
        from korpha.inference.types import CompletionResponse
        self.calls += 1
        return CompletionResponse(
            content=self._text, tool_calls=(),
            input_tokens=10, output_tokens=200, cached_tokens=0,
            cost_usd=Decimal("0.001"),
            provider="stub", model="stub-pro", account_id="t",
            reasoning=None,
        )


class _StubTracker:
    def __init__(self, pool):
        self.pool = pool

    async def complete(self, request, **_kw):
        return await self.pool.complete(request)


def _ctx(session, business, founder, response: str = _VALID_REPORT):
    pool = _StubPool(response)
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=_StubTracker(pool),  # type: ignore[arg-type]
    ), pool


def _add_cost(
    session: Session, business: Business, *,
    days_ago: int, amount: float = 0.001,
    tier: InferenceTier = InferenceTier.WORKHORSE,
) -> None:
    c = Cost(
        business_id=business.id,
        provider="ollama-cloud", model="deepseek-v4-flash",
        tier=tier, input_tokens=100, output_tokens=200,
        cost_usd=Decimal(str(amount)),
    )
    session.add(c); session.commit(); session.refresh(c)
    c.created_at = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    session.add(c); session.commit()


def _add_done_card(
    session: Session, business: Business, *,
    title: str, days_ago_moved: int = 5,
) -> None:
    card = KanbanCard(
        business_id=business.id, title=title,
        column=KanbanColumn.DONE,
    )
    session.add(card); session.commit(); session.refresh(card)
    card.moved_at = datetime.now(tz=timezone.utc) - timedelta(
        days=days_ago_moved,
    )
    session.add(card); session.commit()


def _add_revenue_approval(
    session: Session, business: Business, role_id, *,
    amount_usd: float, days_ago: int = 5,
) -> None:
    a = Approval(
        business_id=business.id,
        agent_role_id=role_id,
        action_class=ActionClass.COMMERCE,
        proposal_summary="paywall",
        action_payload={
            "kind": "create_payment_link",
            "amount_usd": amount_usd,
        },
        status=ApprovalStatus.APPROVED,
    )
    session.add(a); session.commit(); session.refresh(a)
    a.created_at = datetime.now(tz=timezone.utc) - timedelta(
        days=days_ago,
    )
    session.add(a); session.commit()


# ---- happy path ----


@pytest.mark.asyncio
async def test_monthly_review_aggregates_cost_and_kanban(
    session: Session, business: Business, founder: Founder,
) -> None:
    _add_cost(session, business, days_ago=2, amount=0.20)
    _add_cost(session, business, days_ago=15, amount=0.25)
    _add_done_card(session, business, title="ship pricing", days_ago_moved=3)
    _add_done_card(session, business, title="post tweet", days_ago_moved=10)

    skill = default_registry.skills["finance.monthly_review"]
    ctx, _pool = _ctx(session, business, founder)
    result = await skill.run(ctx=ctx, args={})

    assert result.payload["raw_inputs"]["spend_usd"] == pytest.approx(0.45)
    assert result.payload["raw_inputs"]["shipped"] == 2
    assert result.payload["headline"]
    assert result.payload["strategy_proposal"]["next_month_focus"]


@pytest.mark.asyncio
async def test_monthly_review_excludes_old_data(
    session: Session, business: Business, founder: Founder,
) -> None:
    """40 days ago is outside the 30-day window."""
    _add_cost(session, business, days_ago=40, amount=99.0)
    _add_done_card(session, business, title="ancient", days_ago_moved=40)
    _add_cost(session, business, days_ago=5, amount=0.10)

    skill = default_registry.skills["finance.monthly_review"]
    ctx, _ = _ctx(session, business, founder)
    result = await skill.run(ctx=ctx, args={})

    # Only the recent cost made it
    assert result.payload["raw_inputs"]["spend_usd"] == pytest.approx(0.10)
    assert result.payload["raw_inputs"]["shipped"] == 0


@pytest.mark.asyncio
async def test_monthly_review_includes_revenue_from_approved_payment_links(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    _add_revenue_approval(session, business, ceo.id, amount_usd=29.0, days_ago=5)
    _add_revenue_approval(session, business, ceo.id, amount_usd=49.0, days_ago=12)

    skill = default_registry.skills["finance.monthly_review"]
    ctx, _ = _ctx(session, business, founder)
    result = await skill.run(ctx=ctx, args={})
    assert result.payload["raw_inputs"]["revenue_usd"] == pytest.approx(78.0)


@pytest.mark.asyncio
async def test_monthly_review_uses_explicit_period_label(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["finance.monthly_review"]
    ctx, _ = _ctx(session, business, founder)
    result = await skill.run(
        ctx=ctx, args={"period_label": "Q1 2027"},
    )
    assert result.payload["period_label"] == "Q1 2027"


@pytest.mark.asyncio
async def test_monthly_review_zero_data_still_returns_report(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Brand-new business with no spend, no cards, no revenue."""
    skill = default_registry.skills["finance.monthly_review"]
    ctx, _ = _ctx(session, business, founder)
    result = await skill.run(ctx=ctx, args={})
    assert result.payload["headline"]
    assert result.payload["raw_inputs"]["spend_usd"] == 0.0
    assert result.payload["raw_inputs"]["shipped"] == 0


@pytest.mark.asyncio
async def test_monthly_review_isolates_by_business(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Other-business spend/cards must not leak."""
    from uuid import uuid4
    other_biz = Business(
        founder_id=founder.id, name="Other", description="",
    )
    session.add(other_biz); session.commit(); session.refresh(other_biz)
    # Cost belonging to other_biz should not show up in our report
    other_cost = Cost(
        business_id=other_biz.id,
        provider="x", model="x", tier=InferenceTier.PRO,
        input_tokens=0, output_tokens=0, cost_usd=Decimal("999.00"),
    )
    session.add(other_cost); session.commit(); session.refresh(other_cost)
    other_cost.created_at = (
        datetime.now(tz=timezone.utc) - timedelta(days=3)
    )
    session.add(other_cost); session.commit()

    _add_cost(session, business, days_ago=5, amount=0.05)

    skill = default_registry.skills["finance.monthly_review"]
    ctx, _ = _ctx(session, business, founder)
    result = await skill.run(ctx=ctx, args={})
    assert result.payload["raw_inputs"]["spend_usd"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_monthly_review_unparseable_response_raises(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["finance.monthly_review"]
    ctx, _ = _ctx(session, business, founder, response="not json")
    with pytest.raises(SkillError, match="unparseable"):
        await skill.run(ctx=ctx, args={})


def test_monthly_review_registered() -> None:
    assert "finance.monthly_review" in default_registry.skills
