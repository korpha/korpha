"""Hermes-style kanban dispatcher — claim TTL, stale reclaim,
embedded watcher loop.

Ports the reliability core of ``hermes/hermes_cli/kanban_db.py``
``dispatch_once`` + ``release_stale_claims`` + gateway-embedded
``_kanban_dispatcher_watcher`` into Korpha's SQLModel kanban.

Why this exists (and replaces today's ``auto_dispatch.py``):

  Cards sat IN_PROGRESS overnight because the inline trigger fired
  once at claim time and never retried. If the in-process executor
  crashed (provider 503, OOM, Python exception), the card stayed
  claimed forever. Hermes's dispatcher solves this by running on a
  timer that:
    1. Reclaims stale claims (TTL expired → back to READY)
    2. Re-dispatches READY cards to fresh executors
    3. Counts spawn failures per card and auto-blocks after N

Korpha doesn't spawn worker processes (everything runs in the
FastAPI process), so we don't need PID tracking. But TTL + retry +
failure-counter all apply: an async task can hang on a stuck LLM
call, raise an exception that's swallowed, or just take longer
than reasonable to ship.

Dispatcher is gated by ``Settings.kanban_dispatcher_enabled`` and
runs as a FastAPI lifespan background task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlmodel import select

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


_FAILURE_COUNT_KEY = "_dispatch_failure_count"
_LAST_FAILURE_KEY = "_dispatch_last_failure"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def release_stale_claims(
    *,
    engine: "Engine",
    ttl_seconds: int,
) -> int:
    """Find IN_PROGRESS cards whose claim is older than ``ttl_seconds``
    and move them back to READY (clear the claim). Returns the count
    reclaimed.

    Mirrors Hermes's ``release_stale_claims`` — the protection that
    stops cards getting stuck forever when an executor crashes."""
    from sqlmodel import Session

    from korpha.kanban.model import KanbanCard, KanbanColumn

    cutoff = _utc_now() - timedelta(seconds=ttl_seconds)
    reclaimed = 0
    with Session(engine) as session:
        rows = list(session.exec(
            select(KanbanCard)
            .where(KanbanCard.column == KanbanColumn.IN_PROGRESS)
            .where(KanbanCard.claimed_at.is_not(None))  # type: ignore[union-attr]
        ).all())
        for card in rows:
            claimed_at = _aware(card.claimed_at)
            if claimed_at is None:
                continue
            if claimed_at > cutoff:
                continue
            # Stale — return to READY, drop the claim.
            card.column = KanbanColumn.READY
            card.claimed_by_agent_role_id = None
            card.claimed_at = None
            card.moved_at = _utc_now()
            card.updated_at = _utc_now()
            session.add(card)
            reclaimed += 1
            logger.info(
                "dispatcher.reclaim: card %s (%s) returned to "
                "READY after %.0fs stale claim",
                str(card.id)[:8], card.title[:60],
                (_utc_now() - claimed_at).total_seconds(),
            )
        if reclaimed:
            session.commit()
    return reclaimed


def _bump_failure_count(card, error: str) -> int:
    meta = dict(card.metadata_json or {})
    count = int(meta.get(_FAILURE_COUNT_KEY, 0)) + 1
    meta[_FAILURE_COUNT_KEY] = count
    meta[_LAST_FAILURE_KEY] = f"{_utc_now().isoformat()}: {error[:200]}"
    card.metadata_json = meta
    return count


def _reset_failure_count(card) -> None:
    meta = dict(card.metadata_json or {})
    meta.pop(_FAILURE_COUNT_KEY, None)
    meta.pop(_LAST_FAILURE_KEY, None)
    card.metadata_json = meta


def heartbeat_claim(
    *,
    engine: "Engine",
    card_id,
) -> bool:
    """Extend the claim on a currently-claimed card by resetting
    ``claimed_at`` to now. Used by the ``kanban.heartbeat`` skill so
    long-running executors don't get reclaimed mid-flight.

    Returns False if the card isn't IN_PROGRESS or has no claim."""
    from uuid import UUID

    from sqlmodel import Session

    from korpha.kanban.model import KanbanCard, KanbanColumn

    try:
        cid = UUID(str(card_id))
    except (ValueError, TypeError):
        return False
    with Session(engine) as session:
        card = session.get(KanbanCard, cid)
        if card is None:
            return False
        if card.column != KanbanColumn.IN_PROGRESS:
            return False
        if card.claimed_by_agent_role_id is None:
            return False
        card.claimed_at = _utc_now()
        card.updated_at = _utc_now()
        session.add(card)
        session.commit()
        return True


async def dispatch_once(
    *,
    engine: "Engine",
    ttl_seconds: int,
    failure_limit: int = 3,
) -> dict[str, int]:
    """Run one dispatcher tick. Mirrors Hermes's ``dispatch_once``
    minus the worker-process spawn (Korpha runs executors in-process
    via Workforce.dispatch which is called by the inline path).

    Steps:
      1. release_stale_claims — reclaim TTL-expired claims
      2. Auto-block cards that have exceeded failure_limit

    Returns a small counters dict for logging."""
    counters = {"reclaimed": 0, "auto_blocked": 0}
    counters["reclaimed"] = release_stale_claims(
        engine=engine, ttl_seconds=ttl_seconds,
    )

    # Auto-block any READY card that has failed too many times.
    # Important after reclaims so a card stuck in a crash-loop
    # eventually halts.
    from sqlmodel import Session

    from korpha.kanban.model import KanbanCard, KanbanColumn

    with Session(engine) as session:
        rows = list(session.exec(
            select(KanbanCard)
            .where(KanbanCard.column == KanbanColumn.READY)
        ).all())
        for card in rows:
            meta = card.metadata_json or {}
            count = int(meta.get(_FAILURE_COUNT_KEY, 0))
            if count >= failure_limit:
                card.column = KanbanColumn.BLOCKED
                card.moved_at = _utc_now()
                card.updated_at = _utc_now()
                session.add(card)
                counters["auto_blocked"] += 1
                logger.warning(
                    "dispatcher.auto_block: card %s (%s) blocked "
                    "after %d failures",
                    str(card.id)[:8], card.title[:60], count,
                )
        if counters["auto_blocked"]:
            session.commit()
    return counters


async def dispatch_loop(engine: "Engine") -> None:
    """Embedded dispatcher watcher — runs as a FastAPI lifespan
    background task. One tick per ``kanban_dispatch_interval_seconds``
    (default 60s). Failures in one tick don't stop subsequent ticks.

    Gated by ``Settings.kanban_dispatcher_enabled`` (default True).
    """
    from korpha.config import get_settings

    settings = get_settings()
    if not settings.kanban_dispatcher_enabled:
        logger.info(
            "kanban dispatcher: disabled via "
            "KORPHA_KANBAN_DISPATCHER_ENABLED=false"
        )
        return

    interval = max(5, settings.kanban_dispatch_interval_seconds)
    ttl = max(60, settings.kanban_claim_ttl_seconds)
    failure_limit = max(1, settings.kanban_failure_limit)
    logger.info(
        "kanban dispatcher: starting (interval=%ds, ttl=%ds, "
        "failure_limit=%d)", interval, ttl, failure_limit,
    )

    while True:
        try:
            counters = await dispatch_once(
                engine=engine,
                ttl_seconds=ttl,
                failure_limit=failure_limit,
            )
            if counters.get("reclaimed") or counters.get("auto_blocked"):
                logger.info(
                    "kanban dispatcher tick: reclaimed=%d auto_blocked=%d",
                    counters["reclaimed"], counters["auto_blocked"],
                )
        except asyncio.CancelledError:
            logger.info("kanban dispatcher: cancelled, exiting loop")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("kanban dispatcher tick failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


__all__ = [
    "dispatch_loop",
    "dispatch_once",
    "heartbeat_claim",
    "release_stale_claims",
]
