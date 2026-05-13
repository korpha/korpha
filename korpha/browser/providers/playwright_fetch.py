"""Playwright-backed fetch provider.

The simplest useful provider: spin up a headless Chromium, navigate to
``task.start_url``, return the rendered text + page title (+ optional
screenshot). No LLM action loop — for "go look up this URL and tell me
what's there" use cases like competitor pricing scrape, support article
lookup, blog post research.

For multi-step tasks (fill form, click button, paginate) plug in
``BrowserUseProvider`` (separate file) instead, behind the same ABC.

Why a separate "fetch" provider rather than just using browser-use for
everything: cost. browser-use spends LLM tokens on every step. A fetch
that just needs the page contents shouldn't pay for an action loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from korpha.browser.pool import with_browser_slot
from korpha.browser.service import (
    BrowserError,
    BrowserProvider,
    BrowserResult,
    BrowserTask,
)

_DEFAULT_TEXT_LIMIT = 32_000  # chars


@dataclass
class PlaywrightFetchProvider(BrowserProvider):
    """Headless Chromium via Playwright. Fetches a URL and returns text."""

    name: str = "playwright-fetch"
    text_char_limit: int = _DEFAULT_TEXT_LIMIT
    """Trim returned text to keep downstream prompts bounded."""

    user_agent: str | None = None

    _browser: Any = field(default=None, init=False, repr=False)
    _playwright: Any = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def run(self, task: BrowserTask) -> BrowserResult:
        if not task.start_url:
            raise BrowserError(
                "PlaywrightFetchProvider requires task.start_url — it doesn't "
                "navigate from natural language"
            )

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError(
                "playwright not installed. Run: uv pip install playwright "
                "&& playwright install chromium"
            ) from exc

        async with self._lock:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=task.headless
                )

        # Gate concurrent page contexts via the shared pool. Default
        # concurrency is 1 — two parallel agents serialize, not crash
        # Mike's laptop with two chromiums.
        unit_id = getattr(task, "consumer_unit_id", None)
        async with with_browser_slot(consumer_unit_id=unit_id):
            return await self._run_in_context(task)

    async def _run_in_context(self, task: BrowserTask) -> BrowserResult:
        try:
            ctx = await self._browser.new_context(
                user_agent=task.user_agent or self.user_agent,
                extra_http_headers=task.extra_http_headers or None,
            )
            page = await ctx.new_page()
            try:
                await page.goto(
                    task.start_url,
                    timeout=int(task.timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
                title = await page.title()
                text = ""
                if task.extract_text:
                    text = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                    if len(text) > self.text_char_limit:
                        text = text[: self.text_char_limit] + "\n…[truncated]"
                screenshot: bytes | None = None
                if task.take_screenshot:
                    screenshot = await page.screenshot(type="png")
                final_url = page.url
                return BrowserResult(
                    success=True,
                    final_url=final_url,
                    extracted_text=text,
                    title=title,
                    screenshot_png=screenshot,
                    raw={"status": "ok"},
                )
            finally:
                await page.close()
                await ctx.close()
        except BrowserError:
            raise
        except Exception as exc:
            return BrowserResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def close(self) -> None:
        import contextlib

        async with self._lock:
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None


__all__ = ["PlaywrightFetchProvider"]
