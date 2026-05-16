"""In-process heartbeat tick loop.

Launched from the FastAPI lifespan so wakeups + routines + ScriptCron
jobs actually fire while the server is up. Without this loop, anything
scheduled (cron presets, agentless cron jobs, autonomy daemon tick)
sits in the database forever — :func:`HeartbeatService.tick` is never
called and ``last_status`` stays at ``NEVER_RUN``.

Cadence: ``KORPHA_HEARTBEAT_INTERVAL_S`` (default 60s). The interval is
the *floor* between ticks — each tick takes whatever it takes; the next
one fires no sooner than this many seconds after the previous one
completed. We don't try to run ticks concurrently; that would race on
the same ScriptCron rows.

Failure mode: any exception inside the tick is caught + logged, and
the loop continues. A poisoned tick can't permanently wedge the server.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlmodel import Session

logger = logging.getLogger(__name__)


def _interval_seconds() -> float:
    raw = os.environ.get("KORPHA_HEARTBEAT_INTERVAL_S", "60").strip()
    try:
        val = float(raw)
    except ValueError:
        val = 60.0
    # Floor at 5s — anything tighter is almost certainly a misconfig
    # and burns CPU re-running the same drained queue.
    return max(5.0, val)


async def heartbeat_loop() -> None:
    """Tick forever. Cancellation-safe — the lifespan calls
    ``task.cancel()`` on shutdown and the ``CancelledError`` propagates
    out of this coroutine cleanly."""
    from korpha.db._session import get_engine
    from korpha.heartbeats.dispatcher import HeartbeatService

    interval = _interval_seconds()
    logger.info("heartbeat_loop: starting, tick interval=%.0fs", interval)
    engine = get_engine()

    while True:
        started = datetime.now(tz=timezone.utc)
        try:
            with Session(engine) as session:
                svc = HeartbeatService(session=session)
                result = await svc.tick(now=started)
                if (
                    result.fired
                    or result.failed
                    or result.script_cron_ran
                    or result.routines_enqueued
                ):
                    logger.info(
                        "heartbeat tick: fired=%d failed=%d "
                        "enqueued=%d scriptcron=%d",
                        result.fired, result.failed,
                        result.routines_enqueued,
                        result.script_cron_ran,
                    )
        except asyncio.CancelledError:
            logger.info("heartbeat_loop: cancelled, exiting")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "heartbeat_loop: tick raised %s — continuing",
                type(exc).__name__,
            )
        # Sleep AFTER the tick so cancellation during sleep doesn't
        # cut a tick short.
        await asyncio.sleep(interval)


__all__ = ["heartbeat_loop"]
