"""Tests for BudgetService + CostTracker enforcement."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlmodel import Session

from korpha.audit.model import Cost, InferenceTier
from korpha.budgets import (
    BudgetExceededError, BudgetService,
)
from korpha.budgets.model import (
    BudgetPolicy, BudgetScope, BudgetWindow,
)
from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType


def _add_cost(
    session: Session, business: Business, *,
    amount: float,
    days_ago: float = 0.0,
    tier: InferenceTier = InferenceTier.WORKHORSE,
    agent_role_id=None,
) -> None:
    c = Cost(
        business_id=business.id,
        agent_role_id=agent_role_id,
        provider="x", model="y", tier=tier,
        input_tokens=100, output_tokens=200,
        cost_usd=Decimal(str(amount)),
    )
    session.add(c); session.commit(); session.refresh(c)
    if days_ago > 0:
        c.created_at = (
            datetime.now(tz=timezone.utc)
            - timedelta(days=days_ago)
        )
        session.add(c); session.commit()


# ---- create / validate ----


def test_create_business_policy(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    p = svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("5.00"),
        window=BudgetWindow.DAY,
        label="daily cap",
    )
    assert p.id is not None
    assert p.is_active is True
    assert p.scope == BudgetScope.BUSINESS


def test_create_rejects_zero_limit(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    with pytest.raises(ValueError, match="limit_usd"):
        svc.create(
            business_id=business.id,
            scope=BudgetScope.BUSINESS,
            limit_usd=Decimal("0"),
        )


def test_create_agent_role_requires_id(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    with pytest.raises(ValueError, match="agent_role_id"):
        svc.create(
            business_id=business.id,
            scope=BudgetScope.AGENT_ROLE,
            limit_usd=Decimal("1"),
        )


def test_create_tier_requires_tier(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    with pytest.raises(ValueError, match="tier"):
        svc.create(
            business_id=business.id,
            scope=BudgetScope.TIER,
            limit_usd=Decimal("1"),
        )


def test_create_business_rejects_qualifiers(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    with pytest.raises(ValueError, match="neither"):
        svc.create(
            business_id=business.id,
            scope=BudgetScope.BUSINESS,
            limit_usd=Decimal("1"),
            tier="workhorse",
        )


# ---- check_before_complete ----


def test_check_under_cap_passes(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("5.00"),
    )
    _add_cost(session, business, amount=1.0)
    # Should not raise
    svc.check_before_complete(business_id=business.id)


def test_check_over_cap_raises(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("1.00"),
    )
    _add_cost(session, business, amount=1.5)
    with pytest.raises(BudgetExceededError) as exc:
        svc.check_before_complete(business_id=business.id)
    assert exc.value.limit_usd == Decimal("1.00")
    assert exc.value.spent_usd >= Decimal("1.5")


def test_over_cap_auto_pauses_policy(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    p = svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("1.00"),
    )
    _add_cost(session, business, amount=2.0)
    with pytest.raises(BudgetExceededError):
        svc.check_before_complete(business_id=business.id)

    refreshed = session.get(BudgetPolicy, p.id)
    assert refreshed.is_active is False
    assert refreshed.paused_reason == "hard_stop"
    assert refreshed.paused_at is not None


def test_paused_policy_skipped_in_check(
    session: Session, business: Business,
) -> None:
    """Once paused, the same policy doesn't keep raising on
    every subsequent call. (We pause once → next call raises
    because the cap is still breached, not because pause itself
    is the gate. But once Mike resumes, we should pass cleanly.)"""
    svc = BudgetService(session)
    p = svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("1.00"),
    )
    _add_cost(session, business, amount=2.0)
    # Trip + auto-pause
    with pytest.raises(BudgetExceededError):
        svc.check_before_complete(business_id=business.id)
    # Now pause is set; a second check skips the policy entirely
    # (it's not active). No raise.
    svc.check_before_complete(business_id=business.id)


def test_resume_anchors_window_so_next_call_passes(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    p = svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("1.00"),
    )
    _add_cost(session, business, amount=2.0)
    with pytest.raises(BudgetExceededError):
        svc.check_before_complete(business_id=business.id)
    # Resume: pre-pause Cost rows are still in the rolling window
    # but resume() anchors window_start to now so they're excluded.
    svc.resume(p.id)
    # Should not raise — window is empty
    svc.check_before_complete(business_id=business.id)


# ---- scope-specific enforcement ----


def test_agent_role_scope_only_counts_matching_role(
    session: Session, business: Business,
) -> None:
    cmo = AgentRole(
        business_id=business.id, role_type=RoleType.CMO, title="CMO",
    )
    cto = AgentRole(
        business_id=business.id, role_type=RoleType.CTO, title="CTO",
    )
    session.add_all([cmo, cto]); session.commit()
    session.refresh(cmo); session.refresh(cto)

    svc = BudgetService(session)
    svc.create(
        business_id=business.id,
        scope=BudgetScope.AGENT_ROLE,
        agent_role_id=cmo.id,
        limit_usd=Decimal("1.00"),
    )
    # CTO spend doesn't count
    _add_cost(session, business, amount=10.0, agent_role_id=cto.id)
    svc.check_before_complete(
        business_id=business.id, agent_role_id=cto.id,
    )
    # CMO spend trips
    _add_cost(session, business, amount=2.0, agent_role_id=cmo.id)
    with pytest.raises(BudgetExceededError):
        svc.check_before_complete(
            business_id=business.id, agent_role_id=cmo.id,
        )


def test_tier_scope_only_counts_matching_tier(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    svc.create(
        business_id=business.id,
        scope=BudgetScope.TIER,
        tier="pro",
        limit_usd=Decimal("0.50"),
    )
    # Workhorse spend doesn't count
    _add_cost(session, business, amount=2.0, tier=InferenceTier.WORKHORSE)
    svc.check_before_complete(business_id=business.id, tier="workhorse")
    # Pro spend trips
    _add_cost(session, business, amount=1.0, tier=InferenceTier.PRO)
    with pytest.raises(BudgetExceededError):
        svc.check_before_complete(business_id=business.id, tier="pro")


# ---- maybe_pause_after_complete ----


def test_post_complete_pauses_when_just_crossed(
    session: Session, business: Business,
) -> None:
    """Add costs that go from under-cap to over-cap; maybe_pause
    should flip the policy."""
    svc = BudgetService(session)
    p = svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("2.00"),
    )
    _add_cost(session, business, amount=1.0)
    # Under cap — no pause
    paused = svc.maybe_pause_after_complete(business_id=business.id)
    assert paused == []
    # Push over
    _add_cost(session, business, amount=1.5)
    paused = svc.maybe_pause_after_complete(business_id=business.id)
    assert len(paused) == 1
    assert paused[0].id == p.id


# ---- status ----


def test_status_includes_pct_used(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("10.00"),
    )
    _add_cost(session, business, amount=2.5)
    rows = svc.status(business.id)
    assert len(rows) == 1
    assert rows[0].pct_used == 0.25
    assert rows[0].spent_usd == Decimal("2.5")
    assert rows[0].remaining_usd == Decimal("7.5")


def test_status_sorts_by_pct_desc(
    session: Session, business: Business,
) -> None:
    svc = BudgetService(session)
    svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("10.00"),
        label="cool",
    )
    svc.create(
        business_id=business.id,
        scope=BudgetScope.TIER, tier="pro",
        limit_usd=Decimal("1.00"),
        label="hot",
    )
    _add_cost(session, business, amount=0.9, tier=InferenceTier.PRO)
    rows = svc.status(business.id)
    # Pro at 90% should come first
    assert rows[0].policy.label == "hot"


# ---- isolation ----


def test_isolates_by_business(
    session: Session, business: Business, founder: Founder,
) -> None:
    other = Business(
        founder_id=founder.id, name="Other", description="",
    )
    session.add(other); session.commit(); session.refresh(other)
    svc = BudgetService(session)
    svc.create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=Decimal("1.00"),
    )
    # Another business's cost — shouldn't trip our policy
    _add_cost(session, other, amount=99.0)
    svc.check_before_complete(business_id=business.id)


# ---- CostTracker integration ----


@pytest.mark.asyncio
async def test_cost_tracker_raises_on_exceeded(
    session: Session, business: Business,
) -> None:
    """The end-to-end contract: BudgetExceededError surfaces
    out of CostTracker.complete() before the LLM call."""
    from decimal import Decimal as _D
    from korpha.budgets import (
        BudgetService as _BS, BudgetScope, BudgetWindow,
    )
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.types import (
        CompletionRequest, CompletionResponse, Message, Role,
    )

    _BS(session).create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        limit_usd=_D("0.001"),
    )
    _add_cost(session, business, amount=1.0)

    class _FakePool:
        async def complete(self, request):
            raise AssertionError(
                "pool.complete should not have been called",
            )

    tracker = CostTracker(pool=_FakePool())  # type: ignore[arg-type]
    request = CompletionRequest(
        messages=[Message(role=Role.SYSTEM, content="x")],
        tier=InferenceTier.WORKHORSE,
        session_key="t",
        max_tokens=100,
        timeout_seconds=10.0,
    )
    with pytest.raises(BudgetExceededError):
        await tracker.complete(
            request, session=session, business_id=business.id,
        )


# fixture imports
from korpha.identity.model import Founder  # noqa: E402
