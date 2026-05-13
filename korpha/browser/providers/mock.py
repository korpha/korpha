"""Mock browser provider — deterministic, offline.

Used by tests and ``korpha browser test --mock`` to verify wiring
without spinning up Chromium or hitting the network. Returns whatever
the constructor was told to return.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from korpha.browser.service import (
    BrowserError,
    BrowserProvider,
    BrowserResult,
    BrowserTask,
)


@dataclass
class MockBrowserProvider(BrowserProvider):
    """Returns canned BrowserResult. Doesn't touch the network."""

    name: str = "mock"
    canned: BrowserResult | None = None
    """Result to return for every task. Default: a benign success."""

    raise_for_url: dict[str, str] = field(default_factory=dict)
    """If task.start_url matches a key, raise BrowserError(value) instead."""

    calls: list[BrowserTask] = field(default_factory=list)

    async def run(self, task: BrowserTask) -> BrowserResult:
        self.calls.append(task)
        if task.start_url and task.start_url in self.raise_for_url:
            raise BrowserError(self.raise_for_url[task.start_url])
        if self.canned is not None:
            return self.canned
        return BrowserResult(
            success=True,
            final_url=task.start_url,
            extracted_text="(mock) " + task.instruction,
            title="Mock Page",
        )

    async def close(self) -> None:
        return None


__all__ = ["MockBrowserProvider"]
