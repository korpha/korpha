"""Heartbeat dispatcher: scheduling, deduping, routines, recovery."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.db._base import utcnow
from korpha.heartbeats.dispatcher import (
    HandlerContext,
    HandlerRegistry,
    HeartbeatService,
)
from korpha.heartbeats.model import (
    Routine,
    RoutineSchedule,
    Wakeup,
    WakeupStatus,
)


@pytest.mark.asyncio
async def test_schedule_inserts_pending_wakeup(
    session: Session, business: Business
) -> None:
    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    w = svc.schedule(
        business_id=business.id,
        kind="ceo.daily_digest",
        fire_at=utcnow(),
    )
    assert w is not None
    assert w.status == WakeupStatus.PENDING
    assert w.kind == "ceo.daily_digest"


@pytest.mark.asyncio
async def test_dedupe_key_collapses_duplicate_pending(
    session: Session, business: Business
) -> None:
    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    w1 = svc.schedule(
        business_id=business.id,
        kind="x",
        fire_at=utcnow(),
        dedupe_key="dk-1",
    )
    w2 = svc.schedule(
        business_id=business.id,
        kind="x",
        fire_at=utcnow(),
        dedupe_key="dk-1",
    )
    assert w1 is not None
    assert w2 is None
    rows = session.exec(select(Wakeup)).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_handler_runs_and_marks_done(
    session: Session, business: Business
) -> None:
    received: list[Wakeup] = []

    async def handler(ctx: HandlerContext) -> None:
        received.append(ctx.wakeup)

    reg = HandlerRegistry()
    reg.register("test.kind", handler)
    svc = HeartbeatService(session=session, registry=reg)
    svc.schedule(business_id=business.id, kind="test.kind", fire_at=utcnow())

    result = await svc.tick()
    assert result.fired == 1
    assert len(received) == 1
    rows = session.exec(select(Wakeup)).all()
    assert all(w.status == WakeupStatus.DONE for w in rows)


@pytest.mark.asyncio
async def test_handler_failure_marks_failed_with_error(
    session: Session, business: Business
) -> None:
    async def boom(_: HandlerContext) -> None:
        raise RuntimeError("kaboom")

    reg = HandlerRegistry()
    reg.register("test.boom", boom)
    svc = HeartbeatService(session=session, registry=reg)
    svc.schedule(business_id=business.id, kind="test.boom", fire_at=utcnow())

    result = await svc.tick()
    assert result.failed == 1
    [w] = session.exec(select(Wakeup)).all()
    assert w.status == WakeupStatus.FAILED
    assert w.last_error is not None and "kaboom" in w.last_error


@pytest.mark.asyncio
async def test_unknown_handler_kind_is_skipped(
    session: Session, business: Business
) -> None:
    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    svc.schedule(business_id=business.id, kind="unknown.kind", fire_at=utcnow())
    result = await svc.tick()
    assert result.skipped_no_handler == 1
    [w] = session.exec(select(Wakeup)).all()
    # Stays pending so a later deploy with a handler can pick it up.
    assert w.status == WakeupStatus.PENDING


@pytest.mark.asyncio
async def test_future_wakeup_is_not_fired(
    session: Session, business: Business
) -> None:
    fired: list[str] = []

    async def handler(ctx: HandlerContext) -> None:
        fired.append(ctx.wakeup.kind)

    reg = HandlerRegistry()
    reg.register("future", handler)
    svc = HeartbeatService(session=session, registry=reg)
    svc.schedule(
        business_id=business.id,
        kind="future",
        fire_at=utcnow() + timedelta(hours=1),
    )
    result = await svc.tick()
    assert result.fired == 0
    assert fired == []


@pytest.mark.asyncio
async def test_routine_enqueues_when_due(
    session: Session, business: Business
) -> None:
    routine = Routine(
        business_id=business.id,
        name="daily digest",
        kind="ceo.daily_digest",
        schedule_kind=RoutineSchedule.EVERY_SECONDS,
        schedule_value=60,
    )
    session.add(routine)
    session.commit()
    session.refresh(routine)

    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    result = await svc.tick()
    assert result.routines_enqueued == 1
    [w] = session.exec(select(Wakeup)).all()
    assert w.kind == "ceo.daily_digest"
    assert w.routine_id == routine.id


@pytest.mark.asyncio
async def test_routine_does_not_re_enqueue_within_interval(
    session: Session, business: Business
) -> None:
    routine = Routine(
        business_id=business.id,
        name="r",
        kind="x",
        schedule_kind=RoutineSchedule.EVERY_SECONDS,
        schedule_value=3600,  # 1 hour
    )
    session.add(routine)
    session.commit()
    svc = HeartbeatService(session=session, registry=HandlerRegistry())

    first = await svc.tick()
    second = await svc.tick()
    assert first.routines_enqueued == 1
    assert second.routines_enqueued == 0


@pytest.mark.asyncio
async def test_disabled_routine_does_not_fire(
    session: Session, business: Business
) -> None:
    routine = Routine(
        business_id=business.id,
        name="r",
        kind="x",
        schedule_kind=RoutineSchedule.EVERY_SECONDS,
        schedule_value=1,
        enabled=False,
    )
    session.add(routine)
    session.commit()
    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    result = await svc.tick()
    assert result.routines_enqueued == 0


@pytest.mark.asyncio
async def test_stuck_in_flight_recovers_to_pending(
    session: Session, business: Business
) -> None:
    svc = HeartbeatService(
        session=session,
        registry=HandlerRegistry(),
        stuck_after=timedelta(seconds=1),
    )
    # Manually create a stale in_flight wakeup.
    stuck = Wakeup(
        business_id=business.id,
        kind="x",
        fire_at=utcnow() - timedelta(minutes=5),
        status=WakeupStatus.IN_FLIGHT,
    )
    session.add(stuck)
    session.commit()

    result = await svc.tick()
    assert result.recovered == 1
    session.refresh(stuck)
    assert stuck.status == WakeupStatus.PENDING


@pytest.mark.asyncio
async def test_cancel_pending_wakeup(session: Session, business: Business) -> None:
    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    w = svc.schedule(business_id=business.id, kind="x", fire_at=utcnow())
    assert w is not None
    assert svc.cancel(w.id) is True
    session.refresh(w)
    assert w.status == WakeupStatus.CANCELLED


@pytest.mark.asyncio
async def test_dedupe_only_against_pending(
    session: Session, business: Business
) -> None:
    """If a previous wakeup with the same dedupe_key has already fired
    (status=DONE), we should re-enqueue — the dedupe is for in-flight
    duplicates, not for a global dedupe-forever record."""
    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    w1 = svc.schedule(
        business_id=business.id,
        kind="x",
        fire_at=utcnow(),
        dedupe_key="key",
    )
    assert w1 is not None
    w1.status = WakeupStatus.DONE
    session.add(w1)
    session.commit()

    w2 = svc.schedule(
        business_id=business.id,
        kind="x",
        fire_at=utcnow(),
        dedupe_key="key",
    )
    assert w2 is not None


@pytest.mark.asyncio
async def test_tick_runs_due_script_cron_jobs(
    session: Session, business: Business, tmp_path,
) -> None:
    """The heartbeat tick now also runs agentless cron — proves
    the integration without hitting subprocess: we pre-set
    last_run_at far in the past so the job is due, then count
    invocations on a stub run_due_jobs."""
    from korpha.scriptcron import ScriptCron
    from datetime import datetime, timedelta as _td, timezone

    script = tmp_path / "noop.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    script.chmod(0o755)

    job = ScriptCron(
        business_id=business.id, name="t",
        script_path=str(script), cadence="every 1m",
        last_run_at=datetime.now(tz=timezone.utc) - _td(hours=1),
    )
    session.add(job); session.commit(); session.refresh(job)

    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    result = await svc.tick()
    assert result.script_cron_ran == 1


@pytest.mark.asyncio
async def test_tick_handles_scriptcron_failure_gracefully(
    session: Session, business: Business, monkeypatch,
) -> None:
    """A bug in scriptcron's runner must not wedge the heartbeat
    tick — the agent loop has to keep going."""
    def boom(*_a, **_k):
        raise RuntimeError("scriptcron module exploded")

    monkeypatch.setattr(
        "korpha.scriptcron.run_due_jobs", boom,
    )
    svc = HeartbeatService(session=session, registry=HandlerRegistry())
    result = await svc.tick()
    # Tick completed despite the cron explosion
    assert result.script_cron_ran == 0
