"""Persistent CDP-style supervisor for fast JS eval workloads.

The existing :class:`PlaywrightFetchProvider` keeps the browser
instance alive across calls (good — avoids the ~2-second Chromium
startup tax) but spawns a fresh context + page per ``run()``. For
"agent issues 30 quick JS evals against a single SPA" workloads
that page-spawn cost dominates.

This supervisor keeps a single context + page open and exposes a
narrow ``evaluate_js`` method that pipes straight through to
``page.evaluate``. ~10-200x speedup vs the per-call spawn path
depending on how heavy the page is.

Mirrors Hermes PR #23226 — they use raw CDP WebSocket framing;
we use Playwright's evaluate (which itself sits on CDP under the
hood, so the win is mostly the page-reuse, not raw protocol
framing).

Usage::

    supervisor = BrowserEvalSupervisor()
    await supervisor.start(url="https://app.example.com")
    title = await supervisor.evaluate_js("document.title")
    rows  = await supervisor.evaluate_js(
        "Array.from(document.querySelectorAll('tr')).map(t=>t.innerText)"
    )
    await supervisor.close()
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BrowserEvalSupervisor:
    """One long-lived Chromium page; per-evaluate cost is just the
    serialize/IPC round-trip (~5-15ms vs ~200-800ms for spawn-per-eval).

    Single-page only — not a session pool. If you need parallel pages,
    instantiate N supervisors. This matches our browser-concurrency=1
    default; bumping that just bumps your supervisor count.
    """

    headless: bool = True
    user_agent: str | None = None
    extra_http_headers: dict[str, str] | None = None

    _playwright: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)
    _context: Any = field(default=None, init=False, repr=False)
    _page: Any = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False,
    )
    _started: bool = field(default=False, init=False, repr=False)

    async def start(self, *, url: str | None = None) -> None:
        """Launch the browser and (optionally) navigate to a starting
        URL. Idempotent — calling start() twice is a no-op for the
        browser launch; the second navigate replaces the active page."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright not installed. Run: uv pip install "
                "playwright && playwright install chromium",
            ) from exc

        async with self._lock:
            if not self._started:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                )
                self._context = await self._browser.new_context(
                    user_agent=self.user_agent,
                    extra_http_headers=self.extra_http_headers or None,
                )
                self._page = await self._context.new_page()
                self._started = True
            if url:
                await self._page.goto(
                    url, wait_until="domcontentloaded",
                )

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        """Switch the active page to a new URL. Cheaper than spinning
        a new supervisor when the agent walks between pages of the
        same site."""
        if not self._started:
            await self.start(url=url)
            return
        await self._page.goto(url, wait_until=wait_until)

    async def evaluate_js(self, script: str) -> Any:
        """Run a JS expression in the active page and return the
        result. The fast path — ~5-15ms per call vs ~200-800ms for
        spawn-per-eval."""
        if not self._started:
            raise RuntimeError(
                "BrowserEvalSupervisor not started — call start() first",
            )
        return await self._page.evaluate(script)

    async def click(self, selector: str) -> None:
        """Click an element. Convenience for the common case of
        evaluate('document.querySelector(...).click()') which doesn't
        wait for the click handler to settle."""
        if not self._started:
            raise RuntimeError("supervisor not started")
        await self._page.click(selector)

    async def screenshot(self) -> bytes:
        """PNG bytes of the current viewport. Useful for vision-tier
        debugging when the agent isn't sure what state the page is in."""
        if not self._started:
            raise RuntimeError("supervisor not started")
        return await self._page.screenshot(type="png")

    async def page_text(self) -> str:
        """Visible-text dump of the current page. Same as the
        playwright_fetch provider's extract_text path."""
        if not self._started:
            raise RuntimeError("supervisor not started")
        return await self._page.evaluate(
            "() => document.body ? document.body.innerText : ''",
        )

    async def close(self) -> None:
        """Shut down the browser. Idempotent."""
        import contextlib

        async with self._lock:
            if self._page is not None:
                with contextlib.suppress(Exception):
                    await self._page.close()
                self._page = None
            if self._context is not None:
                with contextlib.suppress(Exception):
                    await self._context.close()
                self._context = None
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None
            self._started = False


# Module-level singleton for the simple "one supervisor per process"
# case. Bigger workloads instantiate their own. The pool gate in
# korpha.browser.pool still serializes calls.
_DEFAULT_SUPERVISOR: BrowserEvalSupervisor | None = None


def default_supervisor() -> BrowserEvalSupervisor:
    """Process-wide singleton. Lazy-instantiated."""
    global _DEFAULT_SUPERVISOR
    if _DEFAULT_SUPERVISOR is None:
        _DEFAULT_SUPERVISOR = BrowserEvalSupervisor()
    return _DEFAULT_SUPERVISOR


async def close_default_supervisor() -> None:
    """Shut the singleton — call from app teardown."""
    global _DEFAULT_SUPERVISOR
    if _DEFAULT_SUPERVISOR is not None:
        await _DEFAULT_SUPERVISOR.close()
        _DEFAULT_SUPERVISOR = None


__all__ = [
    "BrowserEvalSupervisor",
    "close_default_supervisor",
    "default_supervisor",
]
