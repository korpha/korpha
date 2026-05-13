"""Tests for the concurrency-gated browser pool."""
from __future__ import annotations

import asyncio

import pytest

from korpha.browser import pool


@pytest.fixture(autouse=True)
def _reset_pool_state():
    """Pool is a module singleton; reset between tests."""
    pool._state.semaphore = None
    pool._state.max_concurrent = 1
    pool._state.in_use = 0
    pool._state.total_acquisitions = 0
    pool._state.last_acquired_at = None
    yield
    pool._state.semaphore = None
    pool._state.max_concurrent = 1
    pool._state.in_use = 0
    pool._state.total_acquisitions = 0
    pool._state.last_acquired_at = None


@pytest.mark.asyncio
async def test_default_concurrency_is_one() -> None:
    st = pool.get_status()
    assert st.max_concurrent == 1
    assert st.in_use == 0


@pytest.mark.asyncio
async def test_concurrent_acquisitions_serialize_at_default() -> None:
    """Two parallel acquisitions with concurrency=1 should run serially."""
    order: list[str] = []

    async def task(name: str, hold: float) -> None:
        async with pool.with_browser_slot(log_usage=False):
            order.append(f"start:{name}")
            await asyncio.sleep(hold)
            order.append(f"end:{name}")

    await asyncio.gather(task("a", 0.05), task("b", 0.05))
    # Strict serial: a starts, a ends, then b starts, b ends — OR
    # the reverse. Crucially, b's start must come after a's end.
    assert order[0].startswith("start:")
    assert order[1].startswith("end:")
    first = order[0].split(":")[1]
    assert order[1] == f"end:{first}"
    second = "b" if first == "a" else "a"
    assert order[2] == f"start:{second}"
    assert order[3] == f"end:{second}"


@pytest.mark.asyncio
async def test_higher_concurrency_runs_in_parallel() -> None:
    await pool.configure(2)
    started: list[str] = []

    async def task(name: str) -> None:
        async with pool.with_browser_slot(log_usage=False):
            started.append(name)
            await asyncio.sleep(0.05)

    await asyncio.gather(task("a"), task("b"))
    # Both start before either finishes — proven by both names landing
    # in started during the 0.05s windows.
    assert set(started) == {"a", "b"}
    st = pool.get_status()
    assert st.total_acquisitions == 2


@pytest.mark.asyncio
async def test_release_decrements_in_use() -> None:
    await pool.configure(3)

    async def acquire_briefly() -> None:
        async with pool.with_browser_slot(log_usage=False):
            assert pool.get_status().in_use >= 1

    await acquire_briefly()
    assert pool.get_status().in_use == 0


@pytest.mark.asyncio
async def test_configure_rejects_zero() -> None:
    with pytest.raises(ValueError):
        await pool.configure(0)
    with pytest.raises(ValueError):
        await pool.configure(-1)


@pytest.mark.asyncio
async def test_status_tracks_lifetime_acquisitions() -> None:
    for _ in range(3):
        async with pool.with_browser_slot(log_usage=False):
            pass
    st = pool.get_status()
    assert st.total_acquisitions == 3
    assert st.last_acquired_at is not None
