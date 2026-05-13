"""Tests for the worker_hired plugin hook."""
from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import RoleType
from korpha.plugins.hooks import (
    HookKind, WorkerHiredEvent, hook_registry,
)


@pytest.fixture(autouse=True)
def _clear_hooks():
    hook_registry.clear()
    yield
    hook_registry.clear()


def test_hook_kind_value() -> None:
    assert HookKind.WORKER_HIRED.value == "worker_hired"


@pytest.mark.asyncio
async def test_hire_triggers_worker_hired_hook(
    session: Session, business: Business,
) -> None:
    """Calling HiringService.hire() with a WORKER role fires
    the WORKER_HIRED hook with a populated event."""
    captured: list[WorkerHiredEvent] = []

    async def listener(event):
        captured.append(event)

    hook_registry.register(
        HookKind.WORKER_HIRED, listener, plugin_name="test",
    )
    hiring = HiringService(session)
    role = hiring.hire(
        business.id, RoleType.WORKER,
        title="Copywriter", specialty="copywriter",
        source="test:hire",
    )

    # Give the scheduled task a tick to run
    await asyncio.sleep(0)
    assert len(captured) == 1
    event = captured[0]
    assert event.agent_role_id == role.id
    assert event.specialty == "copywriter"
    assert event.role_type == "worker"
    assert event.source == "test:hire"


@pytest.mark.asyncio
async def test_hook_passes_founder_id_when_provided(
    session: Session, business: Business,
) -> None:
    captured: list[WorkerHiredEvent] = []

    async def listener(event):
        captured.append(event)

    hook_registry.register(
        HookKind.WORKER_HIRED, listener, plugin_name="t",
    )
    from uuid import uuid4
    fid = uuid4()
    HiringService(session).hire(
        business.id, RoleType.WORKER,
        title="x", specialty="x", founder_id=fid,
    )
    await asyncio.sleep(0)
    assert captured[0].founder_id == fid


@pytest.mark.asyncio
async def test_hook_passes_reason(
    session: Session, business: Business,
) -> None:
    captured: list[WorkerHiredEvent] = []

    async def listener(event):
        captured.append(event)

    hook_registry.register(
        HookKind.WORKER_HIRED, listener, plugin_name="t",
    )
    HiringService(session).hire(
        business.id, RoleType.WORKER,
        title="x", specialty="x",
        reason="we keep doing 5 LinkedIn drafts a week",
    )
    await asyncio.sleep(0)
    assert "LinkedIn" in (captured[0].reason or "")


def test_hook_no_listeners_no_crash(
    session: Session, business: Business,
) -> None:
    """No registered listeners → hire() runs cleanly."""
    role = HiringService(session).hire(
        business.id, RoleType.WORKER,
        title="x", specialty="x",
    )
    assert role.id is not None


@pytest.mark.asyncio
async def test_listener_exception_doesnt_break_hire(
    session: Session, business: Business,
) -> None:
    """A buggy listener gets logged + ignored; the hire still
    succeeds and returns a real role."""
    async def bad_listener(event):
        raise RuntimeError("plugin bug")

    hook_registry.register(
        HookKind.WORKER_HIRED, bad_listener, plugin_name="bad",
    )
    role = HiringService(session).hire(
        business.id, RoleType.WORKER,
        title="x", specialty="x",
    )
    await asyncio.sleep(0)
    assert role.id is not None
