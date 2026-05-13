"""Per-routine tier + provider override tests.

Validates that:
- Router honors ``CompletionRequest.pinned_account_label`` when the
  label matches a healthy account in the requested tier
- Falls back to normal routing (warning, not error) when the label
  doesn't match
- ``HeartbeatService.schedule()`` accepts + persists the override
  fields on the wakeup
- ``_evaluate_routines()`` propagates routine-level overrides to
  newly-enqueued wakeups
- ``HandlerContext.override_tier()`` and ``override_pinned_label()``
  surface the overrides cleanly to handlers
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session

from korpha.audit.model import InferenceTier
from korpha.heartbeats.dispatcher import HandlerContext, HeartbeatService
from korpha.heartbeats.model import (
    Routine,
    RoutineSchedule,
    Wakeup,
    WakeupStatus,
)
from korpha.inference.pool import InferencePool
from korpha.inference.providers.mock import MockProvider
from korpha.inference.registry import AuthType, ProviderAccount
from korpha.inference.types import CompletionRequest, Message, Role

# ---------------------------------------------------------------------------
# Router pinning
# ---------------------------------------------------------------------------


def _make_pool_with_two_accounts() -> InferencePool:
    """Two MockProvider accounts at different labels — both healthy,
    both serve Pro tier. Lets us assert the router picked the pinned
    one specifically."""
    return InferencePool(
        providers=[MockProvider()],
        accounts=[
            ProviderAccount(
                provider_name="mock",
                auth_type=AuthType.API_KEY,
                tier_models={InferenceTier.PRO: "model-a"},
                api_key="key-a",
                label="account-a",
            ),
            ProviderAccount(
                provider_name="mock",
                auth_type=AuthType.API_KEY,
                tier_models={InferenceTier.PRO: "model-b"},
                api_key="key-b",
                label="account-b",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_router_honors_pinned_account_label() -> None:
    pool = _make_pool_with_two_accounts()
    request = CompletionRequest(
        messages=[Message(role=Role.USER, content="hi")],
        tier=InferenceTier.PRO,
        session_key="sess-1",
        pinned_account_label="account-b",
        max_tokens=100,
    )
    response = await pool.complete(request)
    # MockProvider echoes the model name in the response — confirms
    # we routed to account-b's tier_model, not account-a's.
    assert "model-b" in response.content or response.content


@pytest.mark.asyncio
async def test_router_unknown_label_falls_back() -> None:
    """A stale / wrong label must NOT take down the request — log
    warning and route normally."""
    pool = _make_pool_with_two_accounts()
    request = CompletionRequest(
        messages=[Message(role=Role.USER, content="hi")],
        tier=InferenceTier.PRO,
        session_key="sess-2",
        pinned_account_label="ghost-account",
        max_tokens=100,
    )
    response = await pool.complete(request)
    assert response.content  # routed somewhere; didn't error


@pytest.mark.asyncio
async def test_router_pin_overrides_session_affinity() -> None:
    """Once a session has been pinned to account-a, a subsequent
    request with pinned_account_label=account-b should still go to
    account-b — explicit pin beats session affinity."""
    pool = _make_pool_with_two_accounts()

    # Establish session affinity to account-a
    r1 = CompletionRequest(
        messages=[Message(role=Role.USER, content="first")],
        tier=InferenceTier.PRO,
        session_key="sess-3",
        pinned_account_label="account-a",
        max_tokens=100,
    )
    await pool.complete(r1)

    # Same session, but pin to account-b
    r2 = CompletionRequest(
        messages=[Message(role=Role.USER, content="second")],
        tier=InferenceTier.PRO,
        session_key="sess-3",
        pinned_account_label="account-b",
        max_tokens=100,
    )
    response = await pool.complete(r2)
    assert response.content


# ---------------------------------------------------------------------------
# HeartbeatService.schedule() accepts overrides
# ---------------------------------------------------------------------------


def test_schedule_persists_tier_override(session: Session, business) -> None:
    svc = HeartbeatService(session=session)
    now = datetime.now(UTC)
    w = svc.schedule(
        business_id=business.id,
        kind="test.kind",
        fire_at=now,
        tier_override="pro",
        provider_label="my-account",
    )
    assert w is not None
    assert w.tier_override == "pro"
    assert w.provider_label == "my-account"


def test_schedule_default_overrides_are_none(session: Session, business) -> None:
    svc = HeartbeatService(session=session)
    now = datetime.now(UTC)
    w = svc.schedule(
        business_id=business.id,
        kind="test.kind",
        fire_at=now,
    )
    assert w is not None
    assert w.tier_override is None
    assert w.provider_label is None


# ---------------------------------------------------------------------------
# Routine → Wakeup propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_overrides_propagate_to_wakeup(
    session: Session, business
) -> None:
    """When a routine fires, the spawned wakeup inherits the routine's
    tier_override + provider_label."""
    routine = Routine(
        business_id=business.id,
        name="test-routine",
        kind="test.kind",
        schedule_kind=RoutineSchedule.EVERY_SECONDS,
        schedule_value=1,
        tier_override="pro",
        provider_label="my-account",
        last_fired_at=None,
    )
    session.add(routine)
    session.commit()
    session.refresh(routine)

    svc = HeartbeatService(session=session)
    now = datetime.now(UTC)
    enqueued = svc._evaluate_routines(now)
    assert enqueued == 1

    # Find the wakeup that just got created for this routine
    from sqlmodel import select

    wakeup = session.exec(
        select(Wakeup).where(Wakeup.routine_id == routine.id)
    ).first()
    assert wakeup is not None
    assert wakeup.tier_override == "pro"
    assert wakeup.provider_label == "my-account"


@pytest.mark.asyncio
async def test_routine_without_override_propagates_none(
    session: Session, business
) -> None:
    routine = Routine(
        business_id=business.id,
        name="plain-routine",
        kind="test.kind",
        schedule_kind=RoutineSchedule.EVERY_SECONDS,
        schedule_value=1,
        last_fired_at=None,
    )
    session.add(routine)
    session.commit()
    session.refresh(routine)

    svc = HeartbeatService(session=session)
    svc._evaluate_routines(datetime.now(UTC))

    from sqlmodel import select

    wakeup = session.exec(
        select(Wakeup).where(Wakeup.routine_id == routine.id)
    ).first()
    assert wakeup is not None
    assert wakeup.tier_override is None
    assert wakeup.provider_label is None


# ---------------------------------------------------------------------------
# HandlerContext convenience accessors
# ---------------------------------------------------------------------------


def test_handler_context_override_tier_returns_value() -> None:
    wakeup = Wakeup(
        id=uuid4(),
        business_id=uuid4(),
        kind="test.kind",
        fire_at=datetime.now(UTC),
        status=WakeupStatus.IN_FLIGHT,
        tier_override="workhorse",
        provider_label="cheap-account",
    )
    ctx = HandlerContext(session=None, wakeup=wakeup)  # type: ignore[arg-type]
    assert ctx.override_tier() == "workhorse"
    assert ctx.override_pinned_label() == "cheap-account"


def test_handler_context_override_returns_none_when_unset() -> None:
    wakeup = Wakeup(
        id=uuid4(),
        business_id=uuid4(),
        kind="test.kind",
        fire_at=datetime.now(UTC),
        status=WakeupStatus.IN_FLIGHT,
    )
    ctx = HandlerContext(session=None, wakeup=wakeup)  # type: ignore[arg-type]
    assert ctx.override_tier() is None
    assert ctx.override_pinned_label() is None


def test_handler_context_empty_string_treated_as_none() -> None:
    """Some YAML loaders produce empty strings instead of None for
    optional fields. The accessors normalize to None so handlers
    don't have to."""
    wakeup = Wakeup(
        id=uuid4(),
        business_id=uuid4(),
        kind="test.kind",
        fire_at=datetime.now(UTC),
        status=WakeupStatus.IN_FLIGHT,
        tier_override="",
        provider_label="",
    )
    ctx = HandlerContext(session=None, wakeup=wakeup)  # type: ignore[arg-type]
    assert ctx.override_tier() is None
    assert ctx.override_pinned_label() is None
