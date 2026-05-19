"""Tests for /background slash command + spawn service."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from korpha.cofounder.background_slash import (
    BackgroundSlashIntent,
    execute_background_slash_listing,
    is_background_slash,
    parse_background_slash,
)
from korpha.cofounder.background_tasks import (
    BackgroundTaskSpec, cancel_background_task,
    list_active_jobs, list_recent_jobs, spawn_background_task,
)
from korpha.jobs.registry import JobStatus, job_registry


# ---------------------------------------------------------------------------
# Slash parser
# ---------------------------------------------------------------------------


def test_is_background_slash_accepts() -> None:
    assert is_background_slash("/background")
    assert is_background_slash("/background list")
    assert is_background_slash("/background research AI startups")
    assert is_background_slash("  /background status bg-abc  ")


def test_is_background_slash_rejects_lookalikes() -> None:
    assert not is_background_slash("/backgroundjob")
    assert not is_background_slash("/back")
    assert not is_background_slash("background list")
    assert not is_background_slash("")


def test_bare_aliases_to_list() -> None:
    intent = parse_background_slash("/background")
    assert intent.action == "list"


@pytest.mark.parametrize("subcommand", ["list", "help"])
def test_explicit_subcommands(subcommand: str) -> None:
    intent = parse_background_slash(f"/background {subcommand}")
    assert intent.action == subcommand


def test_status_parses_job_id() -> None:
    intent = parse_background_slash("/background status bg-abc123")
    assert intent.action == "status"
    assert intent.job_id == "bg-abc123"


def test_cancel_parses_job_id() -> None:
    intent = parse_background_slash("/background cancel bg-xyz")
    assert intent.action == "cancel"
    assert intent.job_id == "bg-xyz"


def test_spawn_with_free_text() -> None:
    intent = parse_background_slash(
        "/background research top 10 AI agent startups",
    )
    assert intent.action == "spawn"
    assert intent.text == "research top 10 AI agent startups"


def test_non_background_input() -> None:
    intent = parse_background_slash("hello world")
    assert intent.action == "unknown"


# ---------------------------------------------------------------------------
# Listing executor
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    """Each test starts with an empty registry."""
    job_registry.clear()
    yield
    job_registry.clear()


def test_executor_list_empty() -> None:
    intent = parse_background_slash("/background")
    reply = execute_background_slash_listing(intent)
    assert "no background tasks" in reply.lower()


def test_executor_help_text() -> None:
    intent = parse_background_slash("/background help")
    reply = execute_background_slash_listing(intent)
    assert "/background <task>" in reply
    assert "status" in reply
    assert "cancel" in reply


def test_executor_status_unknown_id() -> None:
    intent = parse_background_slash("/background status bg-missing")
    reply = execute_background_slash_listing(intent)
    assert "No background task" in reply


def test_executor_cancel_unknown_id() -> None:
    intent = parse_background_slash("/background cancel bg-missing")
    reply = execute_background_slash_listing(intent)
    assert "Couldn't cancel" in reply


# ---------------------------------------------------------------------------
# Spawn / lifecycle — mock CEO + router
# ---------------------------------------------------------------------------


def _stub_spec(task_text: str = "do something") -> BackgroundTaskSpec:
    """Build a spec with a fake CEO that returns a known response
    and a fake router whose route_outbound just records what got
    posted."""
    biz = MagicMock(id=uuid4())
    founder = MagicMock(id=uuid4())

    ceo = MagicMock()
    ceo.handle = AsyncMock(return_value=MagicMock(
        content="(stub agent reply)", cost_usd=0.01,
    ))

    router = MagicMock()
    router.posted = []

    def _record(*, business_id, founder_id, platform, content, requesting_agent_role_id):
        router.posted.append(content)
    router.route_outbound = _record

    return BackgroundTaskSpec(
        task_text=task_text,
        business=biz, founder=founder,
        thread_id=uuid4(),
        ceo=ceo, router=router, platform="web",
    )


@pytest.mark.asyncio
async def test_spawn_runs_ceo_and_posts_result() -> None:
    spec = _stub_spec("research X")
    job = spawn_background_task(spec)
    assert job.label.startswith("background:")
    assert job.business_id == str(spec.business.id)

    # Wait for the task to finish
    if job._task is not None:
        await job._task

    fresh = job_registry.get(job.id)
    assert fresh.status == JobStatus.COMPLETED
    assert isinstance(fresh.result, dict)
    assert "(stub agent reply)" in fresh.result["content"]

    # Outbound was posted with the completion marker
    assert any(
        "✓ background task" in msg and job.id in msg
        for msg in spec.router.posted
    )


@pytest.mark.asyncio
async def test_spawn_failure_marks_failed_and_posts_failure() -> None:
    spec = _stub_spec("will fail")
    spec.ceo.handle = AsyncMock(side_effect=RuntimeError("boom"))

    job = spawn_background_task(spec)
    if job._task is not None:
        await job._task

    fresh = job_registry.get(job.id)
    assert fresh.status == JobStatus.FAILED
    assert "boom" in fresh.error
    assert any("✗ background task" in m for m in spec.router.posted)


@pytest.mark.asyncio
async def test_cancel_running_task() -> None:
    """Cancellation flips status to CANCELLED and posts a cancellation
    notice (on_complete still runs)."""
    spec = _stub_spec("long task")

    async def _slow(*args, **kwargs):
        await asyncio.sleep(5)
        return MagicMock(content="never reached", cost_usd=0)
    spec.ceo.handle = AsyncMock(side_effect=_slow)

    job = spawn_background_task(spec)
    # Yield to let the task start
    await asyncio.sleep(0.01)
    assert cancel_background_task(job.id) is True

    # Wait for the cancellation to propagate
    try:
        if job._task is not None:
            await job._task
    except asyncio.CancelledError:
        pass

    fresh = job_registry.get(job.id)
    assert fresh.status == JobStatus.CANCELLED


def test_cancel_unknown_returns_false() -> None:
    assert cancel_background_task("bg-nonexistent") is False


@pytest.mark.asyncio
async def test_cancel_non_background_returns_false() -> None:
    # Submit a non-background job (different label prefix)
    async def _coro():
        return "x"

    job = job_registry.submit(_coro(), label="codex: something")
    assert cancel_background_task(job.id) is False
    # Let the submitted task run to completion so we don't leak warnings
    if job._task is not None:
        await job._task


@pytest.mark.asyncio
async def test_list_active_and_recent_filtering() -> None:
    spec_a = _stub_spec("task A")
    spec_b = _stub_spec("task B")
    # Different business — must be filtered out by list_active(business_id=A)
    spec_b.business = MagicMock(id=uuid4())

    job_a = spawn_background_task(spec_a)
    job_b = spawn_background_task(spec_b)

    # Wait for both
    if job_a._task is not None:
        await job_a._task
    if job_b._task is not None:
        await job_b._task

    # No active jobs after completion
    assert list_active_jobs(business_id=spec_a.business.id) == []

    # Recent: both for business A & B respectively
    recent_a = list_recent_jobs(business_id=spec_a.business.id)
    recent_b = list_recent_jobs(business_id=spec_b.business.id)
    assert any(j.id == job_a.id for j in recent_a)
    assert any(j.id == job_b.id for j in recent_b)
    # Cross-tenant: A's listing must NOT include B's job
    assert not any(j.id == job_b.id for j in recent_a)


# ---------------------------------------------------------------------------
# Status reply shows result preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reply_shows_completed_output() -> None:
    spec = _stub_spec("research")
    job = spawn_background_task(spec)
    if job._task is not None:
        await job._task

    intent = BackgroundSlashIntent(
        action="status", job_id=job.id, raw="...",
    )
    reply = execute_background_slash_listing(intent)
    assert job.id in reply
    assert "completed" in reply
    assert "(stub agent reply)" in reply
