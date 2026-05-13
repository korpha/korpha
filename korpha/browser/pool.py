"""Global concurrency-gated browser pool.

Two parallel agents both running a Playwright session at once will each
spin a separate Chromium — on Mike's laptop that's 500MB+ of RAM per
instance. The pool gates concurrent runs through an asyncio.Semaphore
so we bound how many Chromiums can be open simultaneously.

The cap is stored on a ``SharedResource`` row (kind=BROWSER) under
``config['max_concurrent']``. We default to 1 — most solopreneur work
serializes naturally (one CMO scrape, one COO Stripe poll, etc.).
Agencies running a bigger box can bump it from /app/units or
``korpha browser set-concurrency N``.

Usage is logged as ``SharedResourceUsage`` rows so /app/units monthly
review can show "the Marketro Affiliate line spent 47 browser-minutes
this month."
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import UUID

from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.db._base import utcnow
from korpha.db._session import get_engine
from korpha.shared_resources.model import (
    SharedResource, SharedResourceKind, SharedResourceUsage,
)


BROWSER_RESOURCE_NAME = "browser-pool"


@dataclass
class _PoolState:
    semaphore: asyncio.Semaphore | None = None
    max_concurrent: int = 1
    in_use: int = 0
    total_acquisitions: int = 0
    last_acquired_at: datetime | None = None


_state: _PoolState = _PoolState()
_state_lock = asyncio.Lock()


async def configure(max_concurrent: int) -> None:
    """Set the concurrency cap. Safe to call at any time — existing
    in-flight acquisitions keep their slot but the new cap applies to
    future acquisitions."""
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")
    async with _state_lock:
        _state.max_concurrent = max_concurrent
        _state.semaphore = asyncio.Semaphore(max_concurrent)


async def _ensure_semaphore() -> asyncio.Semaphore:
    async with _state_lock:
        if _state.semaphore is None:
            _state.semaphore = asyncio.Semaphore(_state.max_concurrent)
        return _state.semaphore


@asynccontextmanager
async def with_browser_slot(
    *,
    consumer_unit_id: UUID | None = None,
    log_usage: bool = True,
) -> AsyncIterator[None]:
    """Acquire a browser slot from the pool. Releases on context exit.

    ``consumer_unit_id`` attributes the usage to a specific BusinessUnit
    so monthly review can show which lines consumed browser time. None
    means "company-wide / unknown" — logged but not attributed."""
    sem = await _ensure_semaphore()
    started_at = utcnow()
    await sem.acquire()
    _state.in_use += 1
    _state.total_acquisitions += 1
    _state.last_acquired_at = started_at
    try:
        yield
    finally:
        _state.in_use -= 1
        sem.release()
        if log_usage and consumer_unit_id is not None:
            try:
                _record_usage(consumer_unit_id, started_at, utcnow())
            except Exception:
                # Usage logging is best-effort; never fail the caller
                # because we couldn't write an audit row.
                pass


def _record_usage(
    consumer_unit_id: UUID, started_at: datetime, ended_at: datetime,
) -> None:
    """Best-effort: write a SharedResourceUsage row attributing this
    browser slot use to a unit. Resolves the BROWSER SharedResource
    row by name; if not registered yet (first call before init), we
    register it on the active business."""
    duration_seconds = max(0.001, (ended_at - started_at).total_seconds())
    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(select(Business)).first()
        if business is None:
            return  # pre-init, can't attribute
        resource = session.exec(
            select(SharedResource).where(
                SharedResource.business_id == business.id,
                SharedResource.name == BROWSER_RESOURCE_NAME,
            )
        ).first()
        if resource is None:
            resource = SharedResource(
                business_id=business.id,
                kind=SharedResourceKind.BROWSER,
                name=BROWSER_RESOURCE_NAME,
                label="Headless browser pool",
                config={"max_concurrent": _state.max_concurrent},
            )
            session.add(resource)
            session.commit()
            session.refresh(resource)
        resource.last_used_at = ended_at
        session.add(resource)
        session.add(SharedResourceUsage(
            resource_id=resource.id,
            consumer_unit_id=consumer_unit_id,
            used_at=ended_at,
            units_consumed=duration_seconds,
        ))
        session.commit()


@dataclass
class BrowserPoolStatus:
    max_concurrent: int
    in_use: int
    total_acquisitions: int
    last_acquired_at: datetime | None


def get_status() -> BrowserPoolStatus:
    return BrowserPoolStatus(
        max_concurrent=_state.max_concurrent,
        in_use=_state.in_use,
        total_acquisitions=_state.total_acquisitions,
        last_acquired_at=_state.last_acquired_at,
    )


def hydrate_from_db() -> None:
    """Load max_concurrent from the SharedResource row at startup.
    If the row doesn't exist yet, leave the in-memory default
    (1) and the next ``configure`` or first usage will register it."""
    try:
        engine = get_engine()
    except Exception:
        return
    try:
        with Session(engine) as session:
            business = session.exec(select(Business)).first()
            if business is None:
                return
            resource = session.exec(
                select(SharedResource).where(
                    SharedResource.business_id == business.id,
                    SharedResource.name == BROWSER_RESOURCE_NAME,
                )
            ).first()
            if resource is not None and isinstance(resource.config, dict):
                cap = resource.config.get("max_concurrent")
                if isinstance(cap, int) and cap >= 1:
                    _state.max_concurrent = cap
                    _state.semaphore = asyncio.Semaphore(cap)
    except Exception:
        # DB not initialized yet — keep defaults.
        return


async def persist_concurrency(
    new_cap: int, business_id: UUID | None = None,
) -> None:
    """Persist a new concurrency cap to the SharedResource row + update
    the live semaphore. Used by the CLI / UI."""
    if new_cap < 1:
        raise ValueError("max_concurrent must be >= 1")
    await configure(new_cap)
    engine = get_engine()
    with Session(engine) as session:
        if business_id is None:
            business = session.exec(select(Business)).first()
            if business is None:
                return
            business_id = business.id
        resource = session.exec(
            select(SharedResource).where(
                SharedResource.business_id == business_id,
                SharedResource.name == BROWSER_RESOURCE_NAME,
            )
        ).first()
        if resource is None:
            resource = SharedResource(
                business_id=business_id,
                kind=SharedResourceKind.BROWSER,
                name=BROWSER_RESOURCE_NAME,
                label="Headless browser pool",
                config={"max_concurrent": new_cap},
            )
        else:
            cfg = dict(resource.config) if isinstance(resource.config, dict) else {}
            cfg["max_concurrent"] = new_cap
            resource.config = cfg
        session.add(resource)
        session.commit()


__all__ = [
    "BROWSER_RESOURCE_NAME",
    "BrowserPoolStatus",
    "configure",
    "get_status",
    "hydrate_from_db",
    "persist_concurrency",
    "with_browser_slot",
]
