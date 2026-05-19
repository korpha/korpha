"""Browser provider ABC + service layer.

A skill that needs the web declares it via ``ctx.browser`` (added by
the runtime once a provider is wired) and calls
``await ctx.browser.run(BrowserTask(...))``. The provider deals with
spinning up a session, executing, and tearing down.

Mirrors the shape of ``korpha.inference.Provider`` so adding new
backends (Browserbase cloud, Camofox stealth, browser-use's hosted
service) is a one-class affair.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


class BrowserError(RuntimeError):
    """Generic browser failure. Subclasses can specialize per provider."""


@dataclass(frozen=True)
class BrowserTask:
    """One unit of work for a browser provider."""

    instruction: str
    """Natural-language description of the goal. Providers that wrap an
    LLM action loop (browser-use, Browserbase agents) hand this directly
    to the loop. Providers that only fetch (PlaywrightFetchProvider) use
    it as the operator-visible label and ignore it semantically."""

    start_url: str | None = None
    """URL to load first. Required for fetch-style providers, optional
    for action-loop providers that can navigate themselves."""

    headless: bool = True
    """Run hidden by default. Set False if the founder wants to watch."""

    timeout_seconds: float = 60.0
    extract_text: bool = True
    """Whether to populate ``BrowserResult.extracted_text`` from the rendered
    page. Off when the caller only cares about screenshots / cookies."""

    take_screenshot: bool = False
    user_agent: str | None = None
    extra_http_headers: dict[str, str] = field(default_factory=dict)

    consumer_unit_id: UUID | None = None
    """Which BusinessUnit owns this task. Used by the BrowserPool to
    attribute usage in SharedResourceUsage rows for monthly review.
    None means the task is company-wide / unattributed."""

    user_data_dir: str | None = None
    """Path to a persistent Chromium profile. When set, the provider
    uses ``launch_persistent_context`` so cookies + storage survive
    across runs — required for social-media posting where the
    operator logs in once and the agent reuses the session.

    When None, the provider uses a fresh ephemeral context (default
    for scraping)."""

    visual_fallback: bool = False
    """Engage screenshot-driven action mode when the accessibility-
    tree loop can't make progress. Off by default because vision
    calls are ~10x slower + costlier per step. Turn on for sites
    with heavy shadow DOMs (LinkedIn, Instagram) where acc-tree
    returns nothing useful."""

    initial_dwell_seconds: float = 0.0
    """After loading ``start_url``, wait this long before the first
    action step. Useful for letting JS-heavy SPAs settle (LinkedIn
    feeds, IG modals). Most fetches don't need it."""


@dataclass
class BrowserResult:
    success: bool
    final_url: str | None = None
    extracted_text: str = ""
    """Visible text content of the rendered page. Limited by the provider
    to keep prompt budgets sane (PlaywrightFetchProvider trims to ~32k
    chars by default)."""

    title: str | None = None
    screenshot_png: bytes | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras. Don't depend on the shape across providers."""


class BrowserProvider(ABC):
    """Abstract base for any browser backend."""

    name: str

    @abstractmethod
    async def run(self, task: BrowserTask) -> BrowserResult:
        """Execute ``task`` and return the result. Must close any session
        it opens internally before returning so the caller doesn't leak
        Chromium processes between tasks."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down any persistent state (browser pool, http client,
        etc.). Called once on service shutdown. Idempotent."""


@dataclass
class BrowserService:
    """Skill-facing facade. Wraps one or more providers in priority order.

    The first provider whose ``run`` succeeds wins; failures fall through
    to the next. This mirrors how the inference pool retries on rate-limit:
    transient browser failures (CAPTCHA, network blip, single-provider
    throttle) shouldn't take down a skill.
    """

    providers: list[BrowserProvider]
    """At least one. Earlier entries are tried first."""

    async def run(self, task: BrowserTask) -> BrowserResult:
        if not self.providers:
            raise BrowserError("BrowserService configured with no providers")
        # SSRF pre-flight: refuse start_url that resolves to private /
        # cloud-metadata IPs. Doesn't catch in-page redirects — those
        # are the provider's responsibility to re-check on every nav.
        if task.start_url:
            from korpha.security import is_safe_url
            if not is_safe_url(task.start_url):
                return BrowserResult(
                    success=False,
                    error=(
                        f"Refused: {task.start_url!r} resolves to a "
                        f"private/internal/metadata address"
                    ),
                )
        last_error: str | None = None
        last_failed: BrowserResult | None = None
        for provider in self.providers:
            try:
                result = await provider.run(task)
            except BrowserError as exc:
                last_error = f"{provider.name}: {exc}"
                continue
            if result.success:
                return result
            last_error = (
                result.error or f"{provider.name}: provider returned success=False"
            )
            last_failed = result
        # Surface the failed result verbatim when the last provider returned
        # one — that preserves the action log + cumulative cost. Synthesize
        # a bare result only when every provider raised before returning.
        if last_failed is not None:
            return last_failed
        return BrowserResult(success=False, error=last_error or "no provider succeeded")

    async def close(self) -> None:
        for provider in self.providers:
            try:
                await provider.close()
            except Exception:
                continue


__all__ = [
    "BrowserError",
    "BrowserProvider",
    "BrowserResult",
    "BrowserService",
    "BrowserTask",
]
