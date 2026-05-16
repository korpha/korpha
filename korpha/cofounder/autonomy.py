"""Autonomy — does Mike's team auto-grind, and what stops it.

Autonomy is the layer that decides whether the dispatcher should pull
the *next* BACKLOG card into work without Mike typing "go". It composes
three things:

  1. **Mode selector on Business** (:class:`AutonomyMode`):

       - ``off``           — no autonomy. Manual "go" only. Default.
       - ``iterations``    — cap by card-fires per UTC day.
       - ``daily_budget``  — cap by daily $ via :class:`BudgetPolicy`.
       - ``monthly_only``  — cap only by monthly :class:`BudgetPolicy`.

  2. **Counters from existing tables** — no new state to drift:

       - Iterations today  = ``KanbanCardEvent.kind='claim'`` rows
                             with ``occurred_at >= today_utc``.
       - $ today / month   = sum of ``Cost.cost_usd`` (the BudgetPolicy
                             service does this for us).

  3. **BudgetPolicy hard-stops** — modes ``daily_budget`` and
     ``monthly_only`` are real BudgetPolicy rows under the hood, so the
     existing inference-time enforcer trips them. This service only
     reads the policy state — it never duplicates the cap logic.

Surfaces:

  - :func:`evaluate` — one-shot snapshot used by the daemon, the
    dashboard, and the CLI.
  - :func:`check_can_fire` — boolean gate used by the dispatcher
    before claiming the next card. Returns the same snapshot so the
    caller can render the "paused: <reason>" UI.

Backwards compat: ``autonomy_mode IS NULL`` (existing rows pre-
migration) is treated as ``OFF`` so nothing auto-fires on existing
installs until Mike opts in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlmodel import Session, func, select

from korpha.audit.model import Cost
from korpha.budgets.model import BudgetPolicy, BudgetScope, BudgetWindow
from korpha.budgets.service import BudgetService
from korpha.business.model import AutonomyMode, Business
from korpha.credits.model import CreditPool
from korpha.credits.service import CreditService
from korpha.kanban.model import KanbanCardEvent
from korpha.throughput.model import ActionThrottle
from korpha.throughput.service import (
    ActionThrottleService,
    ThrottleStatus,
)

logger = logging.getLogger(__name__)


# Stable label for the BudgetPolicy rows the autonomy panel creates,
# so we can find / update / delete them without hunting on (scope,
# window). The UI is the only writer for these labels — humans
# creating their own per-line BudgetPolicy use their own labels.
DAILY_POLICY_LABEL = "autonomy.daily"
MONTHLY_POLICY_LABEL = "autonomy.monthly"


@dataclass(frozen=True)
class AutonomySnapshot:
    """Everything a caller needs to render the autonomy state."""

    mode: AutonomyMode
    """Resolved mode — never ``None`` (``NULL`` rows resolve to ``OFF``)."""

    # Iterations
    iterations_today: int
    iterations_cap: int | None
    """``None`` when mode != ITERATIONS."""

    # Budgets (USD, current rolling window)
    spent_today_usd: Decimal
    spent_month_usd: Decimal
    daily_cap_usd: Decimal | None
    monthly_cap_usd: Decimal | None
    """Cap values come from the linked BudgetPolicy when one exists,
    else None (no cap configured)."""

    # Action throttles (orthogonal to mode — apply in any mode)
    throttle_statuses: list[ThrottleStatus]
    """One per :class:`ActionThrottle` row for this business, sorted
    by ``pct_used`` desc. Empty list = no throttles configured."""

    credit_pool: CreditPool | None
    """The business's :class:`CreditPool` when one exists. ``None``
    means uncapped — credits not in use for this business."""

    # Why work might be paused right now
    paused: bool
    paused_reason: str | None
    """One of: ``'mode_off'``, ``'iterations_reached'``,
    ``'daily_budget_reached'``, ``'monthly_budget_reached'``,
    ``'<window>_actions_reached'`` (where window is hour/day/week/
    month), or ``None`` when not paused."""


# ---------------------------------------------------------------- helpers


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _today_utc_start(now: datetime) -> datetime:
    """Midnight-UTC anchor for "today". Picks UTC instead of the
    founder's tz on purpose: every install gets the same reset
    boundary, no DST surprises, no per-founder config needed. Trade-
    off acknowledged — Mike in California sees "today" end at 4pm
    local; if that bites we'll revisit with a per-Business tz."""
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


def _resolve_mode(business: Business) -> AutonomyMode:
    """NULL → OFF, defensively normalizing legacy rows."""
    if business.autonomy_mode is None:
        return AutonomyMode.OFF
    return business.autonomy_mode


def _count_iterations_today(
    session: Session, *, business_id: UUID, now: datetime,
) -> int:
    """Count ``kind='claim'`` events (= card fires) for today (UTC)."""
    day_start = _today_utc_start(now)
    stmt = (
        select(func.count())
        .select_from(KanbanCardEvent)
        .where(KanbanCardEvent.business_id == business_id)
        .where(KanbanCardEvent.kind == "claim")
        .where(KanbanCardEvent.occurred_at >= day_start)
    )
    return int(session.exec(stmt).one() or 0)


def _spent_in_window(
    session: Session, *, business_id: UUID, window_start: datetime,
) -> Decimal:
    """Sum Cost rows for the business since ``window_start``. We sum
    the rows in Python instead of in SQL because Decimal handling
    across SQLite / Postgres is uneven."""
    rows = list(
        session.exec(
            select(Cost)
            .where(Cost.business_id == business_id)
            .where(Cost.created_at >= window_start)
        ).all()
    )
    return sum((c.cost_usd for c in rows), Decimal("0"))


def _find_policy(
    session: Session, *, business_id: UUID, label: str,
) -> BudgetPolicy | None:
    return session.exec(
        select(BudgetPolicy)
        .where(BudgetPolicy.business_id == business_id)
        .where(BudgetPolicy.label == label)
        .where(BudgetPolicy.scope == BudgetScope.BUSINESS)
    ).first()


# ---------------------------------------------------------------- public API


def evaluate(
    session: Session,
    *,
    business: Business,
    now: Optional[datetime] = None,
) -> AutonomySnapshot:
    """Return the current autonomy state for a Business.

    Cheap — three queries (event count, two Cost sums, two policy
    lookups). Safe to call on every dispatcher tick and every page
    render. Idempotent — never writes anything."""
    now = now or datetime.now(tz=timezone.utc)
    mode = _resolve_mode(business)

    iterations_today = _count_iterations_today(
        session, business_id=business.id, now=now,
    )
    spent_today = _spent_in_window(
        session, business_id=business.id,
        window_start=_today_utc_start(now),
    )
    spent_month = _spent_in_window(
        session, business_id=business.id,
        window_start=now - timedelta(days=30),
    )

    daily_policy = _find_policy(
        session, business_id=business.id, label=DAILY_POLICY_LABEL,
    )
    monthly_policy = _find_policy(
        session, business_id=business.id, label=MONTHLY_POLICY_LABEL,
    )

    throttle_svc = ActionThrottleService(session)
    throttle_statuses = throttle_svc.status(business.id, now=now)

    credit_svc = CreditService(session)
    # Apply any due monthly refill so the snapshot reflects current
    # balance. Idempotent — no-op when next_refill_at hasn't passed.
    credit_svc.grant_monthly_if_due(business.id, now=now)
    credit_pool = credit_svc.get_pool(business.id)

    iterations_cap: int | None = (
        business.daily_max_iterations
        if mode == AutonomyMode.ITERATIONS else None
    )
    daily_cap = daily_policy.limit_usd if daily_policy else None
    monthly_cap = monthly_policy.limit_usd if monthly_policy else None

    paused, reason = _resolve_pause_state(
        mode=mode,
        iterations_today=iterations_today,
        iterations_cap=iterations_cap,
        daily_cap=daily_cap,
        spent_today=spent_today,
        monthly_cap=monthly_cap,
        spent_month=spent_month,
        daily_policy=daily_policy,
        monthly_policy=monthly_policy,
        throttle_statuses=throttle_statuses,
        credit_pool=credit_pool,
    )

    return AutonomySnapshot(
        mode=mode,
        iterations_today=iterations_today,
        iterations_cap=iterations_cap,
        spent_today_usd=spent_today,
        spent_month_usd=spent_month,
        daily_cap_usd=daily_cap,
        monthly_cap_usd=monthly_cap,
        throttle_statuses=throttle_statuses,
        credit_pool=credit_pool,
        paused=paused,
        paused_reason=reason,
    )


def _resolve_pause_state(
    *,
    mode: AutonomyMode,
    iterations_today: int,
    iterations_cap: int | None,
    daily_cap: Decimal | None,
    spent_today: Decimal,
    monthly_cap: Decimal | None,
    spent_month: Decimal,
    daily_policy: BudgetPolicy | None,
    monthly_policy: BudgetPolicy | None,
    throttle_statuses: list[ThrottleStatus],
    credit_pool: CreditPool | None,
) -> tuple[bool, str | None]:
    """Decide if the team should be paused right now and why.

    Action throttles are evaluated FIRST and apply in every mode —
    if you've hit your weekly action ceiling, the daemon stops
    regardless of whether you're on iterations / daily_budget /
    monthly_only. They're hard load-shaping guardrails.
    """
    # Throttle check runs in every mode (except off, which short-circuits
    # below to mode_off — no point exposing throttle reason then). We use
    # the throttle's paused state OR the count crossing the limit; the
    # service auto-pauses on hard_stop but a fresh evaluate() call may
    # see the count crossed before the pause is committed.
    for ts in throttle_statuses:
        if ts.is_paused or ts.count >= ts.throttle.limit:
            return True, f"{ts.throttle.window.value}_actions_reached"

    # Credit pool — exists only when one is configured. NULL pool = no
    # credit accounting active = no constraint.
    if credit_pool is not None and credit_pool.balance <= 0:
        return True, "credits_exhausted"

    if mode == AutonomyMode.OFF:
        return True, "mode_off"

    if mode == AutonomyMode.ITERATIONS:
        if iterations_cap is not None and iterations_today >= iterations_cap:
            return True, "iterations_reached"
        return False, None

    if mode == AutonomyMode.DAILY_BUDGET:
        # Trust the BudgetPolicy's hard-stop pause state first. The
        # enforcer flips is_active=False when a Cost write pushes
        # spend past limit; we should honor that even if our own
        # rolling-window sum says otherwise (clock skew, late-arriving
        # Cost rows, etc).
        if daily_policy is not None and not daily_policy.is_active:
            return True, "daily_budget_reached"
        if daily_cap is not None and spent_today >= daily_cap:
            return True, "daily_budget_reached"
        if monthly_policy is not None and not monthly_policy.is_active:
            return True, "monthly_budget_reached"
        return False, None

    if mode == AutonomyMode.MONTHLY_ONLY:
        if monthly_policy is not None and not monthly_policy.is_active:
            return True, "monthly_budget_reached"
        if monthly_cap is not None and spent_month >= monthly_cap:
            return True, "monthly_budget_reached"
        return False, None

    return True, "mode_off"


def check_can_fire(
    session: Session,
    *,
    business: Business,
    now: Optional[datetime] = None,
) -> tuple[bool, AutonomySnapshot]:
    """Gate used by the dispatcher before claiming a new card.

    Returns ``(allowed, snapshot)``. ``allowed`` is the inverse of
    ``snapshot.paused``; the snapshot is returned so the caller can
    log / render the reason without re-querying."""
    snap = evaluate(session, business=business, now=now)
    return (not snap.paused), snap


# ---------------------------------------------------------------- writes


def set_mode(
    session: Session,
    *,
    business: Business,
    mode: AutonomyMode,
    daily_max_iterations: int | None = None,
) -> Business:
    """Update autonomy_mode + iteration cap atomically. Validates the
    cap is positive when mode=ITERATIONS."""
    if mode == AutonomyMode.ITERATIONS:
        if daily_max_iterations is None or daily_max_iterations <= 0:
            raise ValueError(
                "autonomy_mode=iterations requires daily_max_iterations > 0",
            )
    business.autonomy_mode = mode
    business.daily_max_iterations = (
        daily_max_iterations if mode == AutonomyMode.ITERATIONS else None
    )
    session.add(business)
    session.commit()
    session.refresh(business)
    return business


def upsert_daily_cap(
    session: Session,
    *,
    business: Business,
    limit_usd: Decimal | None,
) -> BudgetPolicy | None:
    """Create / update / delete the daily $ cap BudgetPolicy.

    ``limit_usd=None`` deletes the policy (used when Mike switches to
    monthly_only or iterations mode and wants to clear the prior cap).
    """
    return _upsert_cap(
        session, business=business, limit_usd=limit_usd,
        window=BudgetWindow.DAY, label=DAILY_POLICY_LABEL,
    )


def upsert_monthly_cap(
    session: Session,
    *,
    business: Business,
    limit_usd: Decimal | None,
) -> BudgetPolicy | None:
    return _upsert_cap(
        session, business=business, limit_usd=limit_usd,
        window=BudgetWindow.MONTH, label=MONTHLY_POLICY_LABEL,
    )


def _upsert_cap(
    session: Session,
    *,
    business: Business,
    limit_usd: Decimal | None,
    window: BudgetWindow,
    label: str,
) -> BudgetPolicy | None:
    existing = _find_policy(
        session, business_id=business.id, label=label,
    )
    if limit_usd is None or limit_usd <= 0:
        if existing is not None:
            session.delete(existing)
            session.commit()
        return None
    if existing is not None:
        existing.limit_usd = Decimal(limit_usd)
        # Resuming a previously hard-stopped policy after the cap was
        # raised — without this, the policy stays paused at the old
        # limit and the UI looks broken ("I raised it but nothing
        # moves"). BudgetService.resume() anchors a fresh window so
        # the prior overage doesn't immediately re-trip.
        if not existing.is_active:
            BudgetService(session).resume(existing.id)
        else:
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing
    return BudgetService(session).create(
        business_id=business.id,
        scope=BudgetScope.BUSINESS,
        window=window,
        limit_usd=Decimal(limit_usd),
        label=label,
    )


__all__ = [
    "AutonomySnapshot",
    "DAILY_POLICY_LABEL",
    "MONTHLY_POLICY_LABEL",
    "check_can_fire",
    "evaluate",
    "set_mode",
    "upsert_daily_cap",
    "upsert_monthly_cap",
]
