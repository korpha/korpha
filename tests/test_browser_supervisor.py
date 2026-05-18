"""Tests for BrowserEvalSupervisor — structural only; the actual
playwright path needs a real browser and is exercised in
integration tests."""
from __future__ import annotations

import pytest

from korpha.browser.supervisor import (
    BrowserEvalSupervisor,
    close_default_supervisor,
    default_supervisor,
)


def test_default_supervisor_singleton():
    a = default_supervisor()
    b = default_supervisor()
    assert a is b


@pytest.mark.anyio
async def test_default_supervisor_close_resets():
    a = default_supervisor()
    await close_default_supervisor()
    b = default_supervisor()
    assert b is not a


@pytest.mark.anyio
async def test_evaluate_before_start_raises():
    sup = BrowserEvalSupervisor()
    with pytest.raises(RuntimeError, match="not started"):
        await sup.evaluate_js("1")


@pytest.mark.anyio
async def test_navigate_before_start_auto_starts(monkeypatch):
    """If the operator calls navigate() before start(), the supervisor
    should auto-start. Tested with a stub to avoid spawning a real
    browser in unit tests."""
    sup = BrowserEvalSupervisor()
    started = {"called": 0}

    async def fake_start(*, url=None):
        started["called"] += 1
        sup._started = True

    monkeypatch.setattr(sup, "start", fake_start)
    await sup.navigate("https://example.com")
    assert started["called"] == 1


@pytest.mark.anyio
async def test_close_idempotent():
    sup = BrowserEvalSupervisor()
    # Not started — close should be a no-op without raising.
    await sup.close()
    await sup.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"
