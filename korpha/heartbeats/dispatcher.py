"""HeartbeatService: schedule wakeups, evaluate routines, dispatch handlers.

Run ``HeartbeatService.tick()`` from a cron job, a long-running daemon, or
the ``korpha tick`` CLI command. Each tick:

1. **Recover stuck wakeups** — anything in ``IN_FLIGHT`` for longer than the
   stuck threshold gets reset to ``PENDING`` so a crash mid-handler doesn't
   wedge the queue.
2. **Evaluate routines** — for each enabled routine whose schedule says it's
   due, enqueue a fresh wakeup (deduped on the routine id).
3. **Drain pending wakeups** — fetch all due, mark in_flight, run the
   handler, transition to done/failed.

Handlers are registered via ``register_handler(kind, fn)``. They receive a
``HandlerContext`` carrying the SQLModel session + the wakeup row, and
return ``None`` on success or raise to mark the wakeup failed.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlmodel import Session, select

from korpha.db._base import as_utc, utcnow
from korpha.heartbeats.model import (
    Routine,
    RoutineSchedule,
    Wakeup,
    WakeupStatus,
)


@dataclass
class HandlerContext:
    """Passed to each handler. Gives access to the live session + wakeup.

    The handler can read ``wakeup.tier_override`` and
    ``wakeup.provider_label`` directly to honor per-routine routing
    overrides when invoking LLM calls — pass them as
    ``CompletionRequest.pinned_account_label`` and override the
    skill's default tier respectively. ``override_tier()`` and
    ``override_pinned_label()`` are convenience accessors.
    """

    session: Session
    wakeup: Wakeup

    def override_tier(self) -> str | None:
        """Tier override for LLM calls fired in this handler, or None
        when no override is set (use the skill's default)."""
        return self.wakeup.tier_override or None

    def override_pinned_label(self) -> str | None:
        """Provider-account label to pin LLM calls to, or None when
        no override is set (use normal session-affinity routing)."""
        return self.wakeup.provider_label or None


HandlerFn = Callable[[HandlerContext], Awaitable[None]]


@dataclass
class HandlerRegistry:
    """Maps wakeup kind → async handler. Module-level default exists for
    convenience; tests should construct their own to stay isolated."""

    handlers: dict[str, HandlerFn] = field(default_factory=dict)

    def register(self, kind: str, fn: HandlerFn) -> None:
        self.handlers[kind] = fn

    def get(self, kind: str) -> HandlerFn | None:
        return self.handlers.get(kind)


_DEFAULT_REGISTRY = HandlerRegistry()


def register_handler(kind: str, fn: HandlerFn) -> None:
    """Module-level convenience for the default registry."""
    _DEFAULT_REGISTRY.register(kind, fn)


def default_registry() -> HandlerRegistry:
    return _DEFAULT_REGISTRY


@dataclass
class HeartbeatTickResult:
    fired: int = 0
    failed: int = 0
    skipped_no_handler: int = 0
    routines_enqueued: int = 0
    recovered: int = 0
    script_cron_ran: int = 0
    """How many agentless ScriptCron jobs ran this tick. Always 0
    on installs that haven't created any cron jobs."""


@dataclass
class HeartbeatService:
    session: Session
    registry: HandlerRegistry = field(default_factory=default_registry)
    stuck_after: timedelta = timedelta(minutes=10)
    """How long an in_flight wakeup sits before we assume the worker died."""

    def schedule(
        self,
        *,
        business_id: UUID,
        kind: str,
        fire_at: datetime,
        payload: dict[str, object] | None = None,
        dedupe_key: str | None = None,
        routine_id: UUID | None = None,
        tier_override: str | None = None,
        provider_label: str | None = None,
    ) -> Wakeup | None:
        """Insert a Wakeup. Returns None when ``dedupe_key`` matches an
        existing pending wakeup of the same kind for the same business
        (caller treats that as a successful no-op).

        ``tier_override`` and ``provider_label`` are routing hints
        propagated to the handler. Most callers leave them None and
        let the heartbeat dispatcher pull them from the parent
        routine; pass them explicitly only when scheduling a one-off
        wakeup that needs a specific tier or account.
        """
        if dedupe_key is not None:
            existing = self.session.exec(
                select(Wakeup)
                .where(Wakeup.business_id == business_id)
                .where(Wakeup.kind == kind)
                .where(Wakeup.dedupe_key == dedupe_key)
                .where(Wakeup.status == WakeupStatus.PENDING)
            ).first()
            if existing is not None:
                return None

        w = Wakeup(
            business_id=business_id,
            kind=kind,
            fire_at=fire_at,
            payload=payload or {},
            dedupe_key=dedupe_key,
            routine_id=routine_id,
            tier_override=tier_override,
            provider_label=provider_label,
        )
        self.session.add(w)
        self.session.commit()
        self.session.refresh(w)
        return w

    def cancel(self, wakeup_id: UUID) -> bool:
        w = self.session.get(Wakeup, wakeup_id)
        if w is None or w.status != WakeupStatus.PENDING:
            return False
        w.status = WakeupStatus.CANCELLED
        self.session.add(w)
        self.session.commit()
        return True

    async def tick(self, now: datetime | None = None) -> HeartbeatTickResult:
        moment = now or utcnow()
        result = HeartbeatTickResult()
        result.recovered = self._recover_stuck(moment)
        result.routines_enqueued = self._evaluate_routines(moment)

        due = self._claim_due(moment)
        for wakeup in due:
            handler = self.registry.get(wakeup.kind)
            if handler is None:
                # No registered handler — leave pending so a future deploy
                # with the handler installed can pick it up. Mark explicitly
                # so we don't loop on it this tick.
                wakeup.status = WakeupStatus.PENDING
                self.session.add(wakeup)
                self.session.commit()
                result.skipped_no_handler += 1
                continue
            try:
                await handler(HandlerContext(session=self.session, wakeup=wakeup))
            except Exception as exc:
                wakeup.status = WakeupStatus.FAILED
                wakeup.last_error = str(exc)[:500]
                wakeup.attempts += 1
                wakeup.fired_at = utcnow()
                self.session.add(wakeup)
                self.session.commit()
                result.failed += 1
                continue
            wakeup.status = WakeupStatus.DONE
            wakeup.fired_at = utcnow()
            wakeup.attempts += 1
            self.session.add(wakeup)
            self.session.commit()
            result.fired += 1

        # Agentless script cron — runs after the agent-loop work
        # since these are programmatic + non-blocking. One bad cron
        # job can't poison the agent loop because run_due_jobs
        # swallows per-job exceptions internally.
        try:
            from korpha.scriptcron import run_due_jobs
            outcomes = await run_due_jobs(self.session, now=moment)
            result.script_cron_ran = len(outcomes)
        except Exception as exc:  # noqa: BLE001
            # Cron-runtime import or unexpected scan error — log and
            # continue. The agent loop must not wedge on a flaky
            # cron implementation.
            import logging as _log
            _log.getLogger(__name__).warning(
                "heartbeat: scriptcron tick errored: %s", exc,
            )
        return result

    def _recover_stuck(self, now: datetime) -> int:
        threshold = now - self.stuck_after
        stmt = (
            select(Wakeup)
            .where(Wakeup.status == WakeupStatus.IN_FLIGHT)
            .where(Wakeup.fire_at <= threshold)
        )
        stuck = list(self.session.exec(stmt).all())
        for w in stuck:
            w.status = WakeupStatus.PENDING
            self.session.add(w)
        if stuck:
            self.session.commit()
        return len(stuck)

    def _evaluate_routines(self, now: datetime) -> int:
        stmt = select(Routine).where(Routine.enabled.is_(True))  # type: ignore[attr-defined]
        enqueued = 0
        for routine in self.session.exec(stmt).all():
            if not _is_routine_due(routine, now):
                continue
            wakeup = self.schedule(
                business_id=routine.business_id,
                kind=routine.kind,
                fire_at=now,
                payload=routine.payload,
                dedupe_key=f"routine:{routine.id}:{int(now.timestamp())}",
                routine_id=routine.id,
                tier_override=routine.tier_override,
                provider_label=routine.provider_label,
            )
            if wakeup is not None:
                routine.last_fired_at = now
                self.session.add(routine)
                self.session.commit()
                enqueued += 1
        return enqueued

    def _claim_due(self, now: datetime) -> list[Wakeup]:
        stmt = (
            select(Wakeup)
            .where(Wakeup.status == WakeupStatus.PENDING)
            .where(Wakeup.fire_at <= now)
            .order_by(Wakeup.fire_at.asc())  # type: ignore[attr-defined]
        )
        due = list(self.session.exec(stmt).all())
        if not due:
            return []
        for w in due:
            w.status = WakeupStatus.IN_FLIGHT
            self.session.add(w)
        self.session.commit()
        return due


def _is_routine_due(routine: Routine, now: datetime) -> bool:
    if routine.schedule_kind == RoutineSchedule.EVERY_SECONDS:
        if routine.last_fired_at is None:
            return True
        last = as_utc(routine.last_fired_at)
        if last is None:
            return True
        return (now - last).total_seconds() >= routine.schedule_value
    return False


__all__ = [
    "HandlerContext",
    "HandlerFn",
    "HandlerRegistry",
    "HeartbeatService",
    "HeartbeatTickResult",
    "default_registry",
    "register_handler",
]
