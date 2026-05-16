"""ActionThrottleService — count / cap / pause / resume action volume.

The counted set: an "action" is one row in any of:
  - ``activity`` (high-level business events: hires, blockers, ships)
  - ``cost`` (one per LLM call)
  - ``kanban_card_event`` (claim/move/specify/create/review_evidence)

Counting the union of these three covers everything the team does
that uses CPU or AI without us having to instrument every code path.
New event sources should write to one of the three tables anyway, so
this counter stays current with no extra wiring.

Window math reuses :mod:`korpha.budgets.service` semantics — rolling
window from ``now - window_hours``, or anchored to
``last_window_start`` if a resume() recently happened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlmodel import Session, func, select

from korpha.audit.model import Activity, Cost
from korpha.budgets.model import BudgetWindow, window_hours
from korpha.kanban.model import KanbanCardEvent
from korpha.throughput.model import ActionThrottle

logger = logging.getLogger(__name__)


class ThroughputExceededError(Exception):
    """Raised when an active throttle has hit its action limit."""

    def __init__(
        self,
        *,
        throttle_id: UUID,
        window: BudgetWindow,
        count: int,
        limit: int,
        label: str = "",
    ) -> None:
        self.throttle_id = throttle_id
        self.window = window
        self.count = count
        self.limit = limit
        self.label = label
        super().__init__(
            f"throughput exceeded: {label or 'actions'} "
            f"({window.value}) — {count} of {limit} cap"
        )


@dataclass(frozen=True)
class ThrottleStatus:
    """Snapshot of one throttle at one moment."""

    throttle: ActionThrottle
    count: int
    pct_used: float
    is_paused: bool

    @property
    def remaining(self) -> int:
        return max(0, self.throttle.limit - self.count)


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; normalize to UTC-aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _window_start(
    throttle: ActionThrottle, *, now: datetime,
) -> datetime:
    """Same anchoring logic as BudgetService._window_start."""
    now = _ensure_aware(now)
    width = timedelta(hours=window_hours(throttle.window))
    if throttle.last_window_start is not None:
        anchored = _ensure_aware(throttle.last_window_start)
        anchored_end = anchored + width
        if anchored_end >= now:
            return anchored
    return now - width


def count_actions_in_window(
    session: Session,
    *,
    business_id: UUID,
    since: datetime,
) -> int:
    """Sum action rows across the three source tables for this business.

    Three COUNT() queries instead of a UNION because SQLModel /
    SQLAlchemy abstract-table COUNTs play more reliably across
    SQLite + Postgres than ad-hoc unions on heterogeneous tables.
    All three tables have business_id + timestamp indexes so each
    query is cheap.
    """
    since = _ensure_aware(since)
    activity_n = int(session.exec(
        select(func.count())
        .select_from(Activity)
        .where(Activity.business_id == business_id)
        .where(Activity.created_at >= since)
    ).one() or 0)
    cost_n = int(session.exec(
        select(func.count())
        .select_from(Cost)
        .where(Cost.business_id == business_id)
        .where(Cost.created_at >= since)
    ).one() or 0)
    kanban_n = int(session.exec(
        select(func.count())
        .select_from(KanbanCardEvent)
        .where(KanbanCardEvent.business_id == business_id)
        .where(KanbanCardEvent.occurred_at >= since)
    ).one() or 0)
    return activity_n + cost_n + kanban_n


@dataclass
class ActionThrottleService:
    """Per-Session throttle operations. Mirrors BudgetService shape."""

    session: Session

    # ---- create / read ----

    def create(
        self,
        *,
        business_id: UUID,
        window: BudgetWindow,
        limit: int,
        label: str = "",
    ) -> ActionThrottle:
        if limit <= 0:
            raise ValueError("throttle: limit must be > 0")
        throttle = ActionThrottle(
            business_id=business_id,
            window=window,
            limit=int(limit),
            label=label,
        )
        self.session.add(throttle)
        self.session.commit()
        self.session.refresh(throttle)
        return throttle

    def list_for_business(
        self,
        business_id: UUID,
        *,
        active_only: bool = False,
    ) -> list[ActionThrottle]:
        stmt = select(ActionThrottle).where(
            ActionThrottle.business_id == business_id,
        )
        if active_only:
            stmt = stmt.where(ActionThrottle.is_active)
        return list(self.session.exec(stmt).all())

    def get(self, throttle_id: UUID) -> ActionThrottle | None:
        return self.session.get(ActionThrottle, throttle_id)

    def find_by_label(
        self, business_id: UUID, label: str,
    ) -> ActionThrottle | None:
        return self.session.exec(
            select(ActionThrottle)
            .where(ActionThrottle.business_id == business_id)
            .where(ActionThrottle.label == label)
        ).first()

    # ---- enforcement ----

    def check_before(
        self,
        *,
        business_id: UUID,
        now: Optional[datetime] = None,
    ) -> None:
        """Raise ThroughputExceededError if any active throttle on
        this business is already at or over its cap. Use this at
        gate points where the caller wants to refuse work proactively
        (the autonomy daemon's pre-claim gate, e.g.)."""
        for throttle in self.list_for_business(
            business_id, active_only=True,
        ):
            spent = self._count(throttle, now=now)
            if spent >= throttle.limit:
                self._auto_pause(throttle, reason="hard_stop")
                raise ThroughputExceededError(
                    throttle_id=throttle.id,
                    window=throttle.window,
                    count=spent,
                    limit=throttle.limit,
                    label=throttle.label,
                )

    def maybe_pause_after(
        self,
        *,
        business_id: UUID,
        now: Optional[datetime] = None,
    ) -> list[ActionThrottle]:
        """After a new action lands, re-check every active throttle
        and pause any that just crossed. Returns the newly-paused
        throttles for the caller to log / notify."""
        paused: list[ActionThrottle] = []
        for throttle in self.list_for_business(
            business_id, active_only=True,
        ):
            spent = self._count(throttle, now=now)
            if spent >= throttle.limit:
                self._auto_pause(throttle, reason="hard_stop")
                paused.append(throttle)
        return paused

    def resume(
        self, throttle_id: UUID, *, now: Optional[datetime] = None,
    ) -> ActionThrottle:
        """Reactivate a paused throttle. Anchors a fresh window so
        the prior overage doesn't immediately re-trip."""
        throttle = self.session.get(ActionThrottle, throttle_id)
        if throttle is None:
            raise KeyError(f"throttle {throttle_id} not found")
        throttle.is_active = True
        throttle.paused_reason = None
        throttle.paused_at = None
        throttle.last_window_start = now or datetime.now(tz=timezone.utc)
        throttle.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(throttle)
        self.session.commit()
        self.session.refresh(throttle)
        return throttle

    def pause(
        self, throttle_id: UUID, *, reason: str = "manual",
    ) -> ActionThrottle:
        throttle = self.session.get(ActionThrottle, throttle_id)
        if throttle is None:
            raise KeyError(f"throttle {throttle_id} not found")
        self._auto_pause(throttle, reason=reason)
        return throttle

    def delete(self, throttle_id: UUID) -> bool:
        throttle = self.session.get(ActionThrottle, throttle_id)
        if throttle is None:
            return False
        self.session.delete(throttle)
        self.session.commit()
        return True

    # ---- status ----

    def status(
        self,
        business_id: UUID,
        *,
        now: Optional[datetime] = None,
    ) -> list[ThrottleStatus]:
        """All throttles + current usage, sorted by pct_used desc."""
        out: list[ThrottleStatus] = []
        for throttle in self.list_for_business(business_id):
            spent = self._count(throttle, now=now)
            limit = max(throttle.limit, 1)
            pct = float(spent) / float(limit)
            out.append(ThrottleStatus(
                throttle=throttle,
                count=spent,
                pct_used=pct,
                is_paused=not throttle.is_active,
            ))
        out.sort(key=lambda s: -s.pct_used)
        return out

    # ---- internals ----

    def _count(
        self,
        throttle: ActionThrottle,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        now = now or datetime.now(tz=timezone.utc)
        start = _window_start(throttle, now=now)
        return count_actions_in_window(
            self.session, business_id=throttle.business_id, since=start,
        )

    def _auto_pause(
        self,
        throttle: ActionThrottle,
        *,
        reason: str,
    ) -> None:
        """Mark throttle paused. Idempotent."""
        if not throttle.is_active and throttle.paused_reason == reason:
            return
        throttle.is_active = False
        throttle.paused_reason = reason
        throttle.paused_at = datetime.now(tz=timezone.utc)
        throttle.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(throttle)
        self.session.commit()
        if reason == "hard_stop":
            logger.warning(
                "throttle hard-stop: %s (%s) at %d / %d",
                throttle.label or "actions",
                throttle.window.value,
                self._count(throttle), throttle.limit,
            )


__all__ = [
    "ActionThrottleService",
    "ThroughputExceededError",
    "ThrottleStatus",
    "count_actions_in_window",
]
