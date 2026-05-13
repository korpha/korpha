"""Tests for per-BusinessUnit budget caps + currency display."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine

from korpha.audit.model import Cost, InferenceTier
from korpha.budgets import BudgetScope, BudgetService, BudgetWindow
from korpha.budgets.model import BudgetPolicy
from korpha.business.model import Business
from korpha.business_units.model import BusinessUnit, BusinessUnitKind
from korpha.identity.model import Founder
import korpha.db.registry  # noqa: F401 — register all models


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_business_with_units(session: Session) -> tuple[Business, BusinessUnit, BusinessUnit]:
    founder = Founder(name="Mike", email="m@x.com")
    session.add(founder)
    session.commit()
    session.refresh(founder)
    biz = Business(name="Marketro", founder_id=founder.id)
    session.add(biz)
    session.commit()
    session.refresh(biz)
    pod = BusinessUnit(
        business_id=biz.id, name="POD", slug="pod",
        kind=BusinessUnitKind.LINE, memory_namespace_id=uuid4(),
    )
    kdp = BusinessUnit(
        business_id=biz.id, name="KDP", slug="kdp",
        kind=BusinessUnitKind.LINE, memory_namespace_id=uuid4(),
    )
    session.add_all([pod, kdp])
    session.commit()
    session.refresh(pod)
    session.refresh(kdp)
    return biz, pod, kdp


def test_create_business_unit_scoped_policy(session: Session) -> None:
    biz, pod, _ = _seed_business_with_units(session)
    svc = BudgetService(session)
    policy = svc.create(
        business_id=biz.id,
        scope=BudgetScope.BUSINESS_UNIT,
        business_unit_id=pod.id,
        limit_usd=Decimal("10.00"),
        window=BudgetWindow.DAY,
        label="POD daily",
    )
    assert policy.scope == BudgetScope.BUSINESS_UNIT
    assert policy.business_unit_id == pod.id


def test_business_unit_scope_requires_unit_id(session: Session) -> None:
    biz, _, _ = _seed_business_with_units(session)
    svc = BudgetService(session)
    with pytest.raises(ValueError, match="business_unit_id"):
        svc.create(
            business_id=biz.id,
            scope=BudgetScope.BUSINESS_UNIT,
            limit_usd=Decimal("1"),
        )


def test_business_scope_rejects_unit_id(session: Session) -> None:
    biz, pod, _ = _seed_business_with_units(session)
    svc = BudgetService(session)
    with pytest.raises(ValueError, match="business"):
        svc.create(
            business_id=biz.id,
            scope=BudgetScope.BUSINESS,
            business_unit_id=pod.id,
            limit_usd=Decimal("1"),
        )


def test_pod_cap_does_not_count_kdp_spend(session: Session) -> None:
    """The whole point: POD cap should only see POD costs, not KDP."""
    biz, pod, kdp = _seed_business_with_units(session)
    now = datetime.now(timezone.utc)

    # 7 USD on KDP, 3 USD on POD. POD cap is 5 USD/day — should NOT trip.
    session.add_all([
        Cost(
            business_id=biz.id, business_unit_id=kdp.id,
            cost_usd=Decimal("7.00"),
            input_tokens=100, output_tokens=50,
            tier=InferenceTier.WORKHORSE.value,
            provider="x", model="x", created_at=now,
        ),
        Cost(
            business_id=biz.id, business_unit_id=pod.id,
            cost_usd=Decimal("3.00"),
            input_tokens=100, output_tokens=50,
            tier=InferenceTier.WORKHORSE.value,
            provider="x", model="x", created_at=now,
        ),
    ])
    session.commit()

    svc = BudgetService(session)
    policy = svc.create(
        business_id=biz.id,
        scope=BudgetScope.BUSINESS_UNIT,
        business_unit_id=pod.id,
        limit_usd=Decimal("5.00"),
    )
    # Should NOT raise — POD only spent $3
    svc.check_before_complete(
        business_id=biz.id, business_unit_id=pod.id, now=now,
    )

    # Add $3 more to POD → over the $5 cap
    session.add(Cost(
        business_id=biz.id, business_unit_id=pod.id,
        cost_usd=Decimal("3.00"),
        input_tokens=10, output_tokens=10,
        tier=InferenceTier.WORKHORSE.value,
        provider="x", model="x", created_at=now,
    ))
    session.commit()

    from korpha.budgets.service import BudgetExceededError
    with pytest.raises(BudgetExceededError):
        svc.check_before_complete(
            business_id=biz.id, business_unit_id=pod.id, now=now,
        )


def test_currency_round_trip() -> None:
    """display→usd then usd→display should return the original."""
    from korpha.budgets.currency import (
        display_to_usd, format_amount, usd_to_display,
    )
    import os

    os.environ["KORPHA_DISPLAY_CURRENCY"] = "EUR"
    os.environ["KORPHA_USD_TO_DISPLAY_RATE"] = "0.93"
    try:
        # 100 USD → 93 EUR
        eur = usd_to_display(Decimal("100"))
        assert abs(eur - Decimal("93")) < Decimal("0.01")
        # 93 EUR → 100 USD (round trip)
        back = display_to_usd(eur)
        assert abs(back - Decimal("100")) < Decimal("0.01")
        # Format with symbol
        s = format_amount(Decimal("100"))
        assert "€" in s
        assert "93" in s
    finally:
        del os.environ["KORPHA_DISPLAY_CURRENCY"]
        del os.environ["KORPHA_USD_TO_DISPLAY_RATE"]


def test_currency_default_usd_passthrough() -> None:
    """With default settings, display = USD = no conversion."""
    from korpha.budgets.currency import format_amount, usd_to_display
    assert usd_to_display(Decimal("42")) == Decimal("42")
    assert "42" in format_amount(Decimal("42"))


def test_unknown_currency_falls_back_to_iso_code() -> None:
    """Currencies we don't have a symbol for still render legibly."""
    from korpha.budgets.currency import format_amount
    import os
    os.environ["KORPHA_DISPLAY_CURRENCY"] = "ZWL"
    os.environ["KORPHA_USD_TO_DISPLAY_RATE"] = "322.0"
    try:
        s = format_amount(Decimal("1"))
        assert "ZWL" in s
    finally:
        del os.environ["KORPHA_DISPLAY_CURRENCY"]
        del os.environ["KORPHA_USD_TO_DISPLAY_RATE"]
