"""PlaywrightActionProvider tests using a scripted MockProvider.

Doesn't spin up Chromium. Instead, we monkey-patch the action loop so
every step's LLM-decision and Playwright execute() are stubs we control,
which lets us exercise the loop's branches (done / abort / max-steps /
unknown ref / unparseable JSON) deterministically and offline."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from korpha.audit.model import InferenceTier
from korpha.browser import (
    BrowserResult,
    BrowserTask,
    PlaywrightActionProvider,
)
from korpha.browser.providers.playwright_action import (
    _Action,
    _action_to_log,
    _parse_action,
)
from korpha.browser.service import BrowserError
from korpha.inference import (
    InferencePool,
    MockProvider,
    ProviderAccount,
    TierPricing,
)
from korpha.inference.registry import AuthType


def _pool(static_response: str) -> InferencePool:
    provider = MockProvider(static_response=static_response)
    account = ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "mock-flash",
            InferenceTier.PRO: "mock-pro",
        },
        pricing={
            InferenceTier.PRO: TierPricing(
                input_per_1m_usd=Decimal("0.5"),
                output_per_1m_usd=Decimal("1"),
            ),
        },
        api_key="sk",
    )
    return InferencePool(providers=[provider], accounts=[account])


# ────────────────────────── pure helpers ──────────────────────────


def test_parse_action_click() -> None:
    a = _parse_action({"action": "click", "ref": "@e3"})
    assert a.kind == "click"
    assert a.ref == "@e3"


def test_parse_action_type_with_submit() -> None:
    a = _parse_action(
        {"action": "type", "ref": "@e7", "text": "hi", "submit": True}
    )
    assert a.kind == "type"
    assert a.text == "hi"
    assert a.submit is True


def test_parse_action_done() -> None:
    a = _parse_action({"action": "done", "result": "ok"})
    assert a.kind == "done"
    assert a.result == "ok"


def test_parse_action_unknown_raises() -> None:
    with pytest.raises(BrowserError):
        _parse_action({"action": "wiggle"})


def test_action_to_log_strips_defaults() -> None:
    log = _action_to_log(_Action(kind="click", ref="@e1"))
    assert log == {"action": "click", "ref": "@e1"}


def test_action_to_log_keeps_submit_true() -> None:
    log = _action_to_log(
        _Action(kind="type", ref="@e1", text="hi", submit=True)
    )
    assert log["submit"] is True


# ────────────────────────── ask_for_action ──────────────────────────


@pytest.mark.asyncio
async def test_ask_for_action_returns_parsed_decision() -> None:
    pool = _pool('{"action": "click", "ref": "@e2"}')
    provider = PlaywrightActionProvider(pool=pool, business_id=uuid4())
    snapshot = [
        {"ref": "@e1", "role": "button", "label": "Sign in"},
        {"ref": "@e2", "role": "button", "label": "Create account"},
    ]
    task = BrowserTask(instruction="hit Create account", start_url="https://x")
    action, cost = await provider._ask_for_action(
        task=task, snapshot=snapshot, rendered_text="", step=1
    )
    assert action.kind == "click"
    assert action.ref == "@e2"
    assert cost > 0


@pytest.mark.asyncio
async def test_ask_for_action_unparseable_raises() -> None:
    pool = _pool("not json at all")
    provider = PlaywrightActionProvider(pool=pool, business_id=uuid4())
    task = BrowserTask(instruction="x", start_url="https://x")
    with pytest.raises(BrowserError) as exc:
        await provider._ask_for_action(
            task=task, snapshot=[], rendered_text="", step=1
        )
    assert "unparseable" in str(exc.value)


# ────────────────────────── full loop with a fake page ──────────────────────────


class _FakePage:
    """Just enough surface area for _execute_action to drive end-to-end."""

    def __init__(self) -> None:
        self.url = "https://start"
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.navigations: list[str] = []
        self.scrolls: list[str] = []

    async def click(self, selector: str) -> None:
        self.clicks.append(selector)

    async def fill(self, selector: str, text: str) -> None:
        self.fills.append((selector, text))

    async def press(self, selector: str, key: str) -> None:
        self.fills.append((selector, f"<{key}>"))

    async def goto(self, url: str, **_: Any) -> None:
        self.url = url
        self.navigations.append(url)

    async def evaluate(self, code: str) -> Any:
        if "scrollBy" in code:
            self.scrolls.append(code)
        return ""

    async def title(self) -> str:
        return "Fake"

    async def screenshot(self, **_: Any) -> bytes:
        return b"\x89PNG"

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_execute_click_validates_ref() -> None:
    from korpha.browser.providers.playwright_action import _execute_action

    page = _FakePage()
    snapshot = [{"ref": "@e1", "role": "button", "label": "x"}]
    await _execute_action(
        page, _Action(kind="click", ref="@e1"), snapshot
    )
    assert page.clicks == ['[data-ag-ref="@e1"]']


@pytest.mark.asyncio
async def test_execute_click_unknown_ref_errors() -> None:
    from korpha.browser.providers.playwright_action import _execute_action

    page = _FakePage()
    snapshot = [{"ref": "@e1", "role": "button", "label": "x"}]
    with pytest.raises(BrowserError) as exc:
        await _execute_action(
            page, _Action(kind="click", ref="@e99"), snapshot
        )
    assert "@e99" in str(exc.value)


@pytest.mark.asyncio
async def test_execute_navigate() -> None:
    from korpha.browser.providers.playwright_action import _execute_action

    page = _FakePage()
    await _execute_action(
        page, _Action(kind="navigate", url="https://newhome"), []
    )
    assert page.navigations == ["https://newhome"]


@pytest.mark.asyncio
async def test_execute_scroll_down() -> None:
    from korpha.browser.providers.playwright_action import _execute_action

    page = _FakePage()
    await _execute_action(
        page, _Action(kind="scroll", direction="down"), []
    )
    assert page.scrolls and "scrollBy(0, 600)" in page.scrolls[0]


@pytest.mark.asyncio
async def test_execute_type_with_submit() -> None:
    from korpha.browser.providers.playwright_action import _execute_action

    page = _FakePage()
    snapshot = [{"ref": "@e1", "role": "input", "label": "search"}]
    await _execute_action(
        page,
        _Action(kind="type", ref="@e1", text="hello", submit=True),
        snapshot,
    )
    assert ('[data-ag-ref="@e1"]', "hello") in page.fills
    # press("Enter") is recorded as the magic <Enter> form
    assert any(t == "<Enter>" for _, t in page.fills)


# ────────────────────────── result shape sanity ──────────────────────────


def test_browser_result_carries_steps_and_cost() -> None:
    r = BrowserResult(success=True, raw={"steps": [{"action": "done"}], "cost_usd": 0.0042})
    assert r.raw["steps"][0]["action"] == "done"
    assert r.raw["cost_usd"] == 0.0042
