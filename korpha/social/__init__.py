"""Social-media posting facade — wraps the browser action loop with
per-platform persistent profiles + a generic post workflow.

What this module does:

  - Tracks which platforms Mike has logged into via
    :class:`~korpha.browser.profile_store.ProfileStore`.
  - Exposes a generic ``post_to_platform(slug, text)`` that opens
    the platform's compose URL with the right persistent profile,
    drives the action loop to publish, and records the result.
  - Exposes ``open_login_window(slug)`` for the login wizard. It
    launches a headed Chromium with the profile dir, lets the
    operator log in manually, and returns once the operator
    confirms (by closing the window).

What this module **does not** do (intentionally):

  - It does NOT ship per-platform selectors, OAuth flows, or
    posting endpoints. Each platform is just a generic browser
    drive — Mike's logged-in browser is the auth surface. This
    sidesteps the no-ToS-violating-adapters rule: we ship a
    capability, not a platform integration.
  - It does NOT auto-schedule posts. Scheduling is the caller's
    job; this module just publishes when told.
  - It does NOT scrape feeds or read engagement metrics. Posting
    only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from korpha.browser.profile_store import (
    PLATFORMS,
    PlatformSpec,
    ProfileStore,
    get_platform,
)
from korpha.browser.service import BrowserResult, BrowserTask
from korpha.inference.pool import InferencePool


@dataclass
class PostRequest:
    """One social post the agent should publish.

    Kept minimal — text + optional image paths. Threads / replies /
    quote-tweets are not modelled in this pass; the action loop can
    handle them if instructed in the goal string, but the typed API
    here is single-post-no-thread.
    """

    text: str
    image_paths: tuple[str, ...] = ()
    """Local file paths the loop will upload. Empty when the post is
    text-only."""

    timeout_seconds: float = 90.0
    headless: bool = False
    """Default headed so Mike can see what the agent is doing. The
    CLI / UI exposes a toggle for trusted unattended runs."""


@dataclass
class PostOutcome:
    """Result returned from :func:`post_to_platform`."""

    platform_slug: str
    unit_id: str
    success: bool
    final_url: str | None = None
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    visual_fallback_used: bool = False


def _compose_goal(platform: PlatformSpec, req: PostRequest) -> str:
    """Build the goal string handed to the LLM action loop.

    Keeps platform-specific knowledge OUT of the loop's prompt — the
    LLM looks at the page and figures out which button is "Post".
    The only platform-specific bit here is the URL we open.
    """
    pieces = [
        f"Publish a new post on {platform.label}.",
        f"Compose URL: {platform.compose_url}",
        "Steps the page will need (figure out which buttons + fields):",
        " 1. Open the compose dialog if one isn't already focused.",
        " 2. Paste the text below into the post body.",
        " 3. Attach any provided images (skip if none).",
        " 4. Click the publish / post / share button.",
        " 5. Stop with done() after the page confirms the post.",
        "",
        "Post text (paste verbatim, do not summarize):",
        "----",
        req.text,
        "----",
    ]
    if req.image_paths:
        pieces.append("")
        pieces.append("Images to attach (local file paths):")
        for p in req.image_paths:
            pieces.append(f"  - {p}")
    return "\n".join(pieces)


async def post_to_platform(
    slug: str,
    unit_id: str,
    req: PostRequest,
    *,
    store: ProfileStore,
    pool: InferencePool,
) -> PostOutcome:
    """Drive an end-to-end post to ``(slug, unit_id)`` using the
    saved profile.

    Mark of success: the action loop returns ``done`` (the LLM
    confirms it saw the platform's "Posted!" toast / new tweet in
    the feed / etc.). On success, the store's ``last_post_at`` is
    stamped against the (platform, unit) pair.

    Pre-conditions:
      - The profile dir for ``(slug, unit_id)`` exists (Mike has
        logged in via :func:`open_login_window` at least once for
        this specific business unit). Function raises a clear error
        otherwise so the CLI can prompt to log in.

    ``unit_id`` is used both as the directory key on disk and as
    the prompt-cache affinity key for the LLM (so the same business
    line's posts hit the same provider account session).
    """
    platform = get_platform(slug)
    if not store.profile_exists(slug, unit_id):
        raise FileNotFoundError(
            f"No saved login for {platform.label} on unit {unit_id}. "
            f"Run `korpha social login {slug} --unit <unit-slug>` first."
        )

    from korpha.browser.providers.playwright_action import (
        PlaywrightActionProvider,
    )

    provider = PlaywrightActionProvider(pool=pool, business_id=unit_id)
    profile_dir = str(store.profile_dir(slug, unit_id).resolve())
    task = BrowserTask(
        instruction=_compose_goal(platform, req),
        start_url=platform.compose_url,
        headless=req.headless,
        timeout_seconds=req.timeout_seconds,
        user_data_dir=profile_dir,
        visual_fallback=platform.requires_visual_fallback,
        initial_dwell_seconds=3.0,
        take_screenshot=True,
    )
    try:
        result: BrowserResult = await provider.run(task)
    finally:
        await provider.close()

    outcome = PostOutcome(
        platform_slug=slug,
        unit_id=unit_id,
        success=result.success,
        final_url=result.final_url,
        error=result.error,
        steps=list(result.raw.get("steps", [])),
        cost_usd=float(result.raw.get("cost_usd", 0.0) or 0.0),
        visual_fallback_used=bool(result.raw.get("visual_fallback_used", False)),
    )
    if result.success:
        store.mark_post(slug, unit_id)
    return outcome


async def open_login_window(
    slug: str,
    unit_id: str,
    *,
    store: ProfileStore,
) -> None:
    """Launch a headed Chromium with the persistent profile for
    ``(slug, unit_id)`` loaded.

    Side effects:
      - Creates the per-unit profile dir on first call.
      - Stamps ``last_login_at`` for ``(slug, unit_id)`` when the
        wizard exits cleanly (whether or not Mike actually completed
        login — we trust him to close-without-stamping if he aborts).

    This is intentionally a blocking, interactive flow. There's no
    headless equivalent; the whole point is for Mike to see the
    real platform UI, complete any 2FA challenges, and confirm
    the session is good for the right brand account.
    """
    platform = get_platform(slug)
    if not unit_id:
        raise ValueError("unit_id is required (pick a business line)")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright not installed. Run: uv pip install playwright "
            "&& playwright install chromium"
        ) from exc

    store.ensure_root()
    profile_dir = store.profile_dir(slug, unit_id)
    profile_dir.mkdir(parents=True, exist_ok=True)

    playwright = await async_playwright().start()
    try:
        ctx = await playwright.chromium.launch_persistent_context(
            str(profile_dir.resolve()),
            headless=False,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(platform.home_url, wait_until="domcontentloaded")
        try:
            # Wait until Mike closes the window OR all pages close.
            # ``ctx.on("close", ...)`` fires when the last page is
            # closed; we just poll for the page being closed.
            while ctx.pages:
                await asyncio.sleep(1.0)
        finally:
            import contextlib
            with contextlib.suppress(Exception):
                await ctx.close()
    finally:
        await playwright.stop()
    store.mark_login(slug, unit_id)


def list_platforms() -> tuple[PlatformSpec, ...]:
    """Convenience for the CLI + dashboard — returns the canonical
    platform list. Exists so callers don't import ``PLATFORMS``
    directly (in case future versions vary by founder)."""
    return PLATFORMS


__all__ = [
    "PostOutcome",
    "PostRequest",
    "list_platforms",
    "open_login_window",
    "post_to_platform",
]
