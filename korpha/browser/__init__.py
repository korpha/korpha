"""Browser automation: agent-controllable web browsing.

Two-tier design mirroring how Hermes ships browser tooling:

  - **Provider**: the transport. Owns a browser session (local Chromium
    via Playwright today; Browserbase / Camofox / agent-browser CLI can
    plug in later). Knows how to ``run`` a BrowserTask.
  - **BrowserService**: the agent-facing API. Skills call it to run a
    task without caring whether it's local-fetch, local-action-loop,
    or cloud.

Provider lineup today:
  - ``MockBrowserProvider`` — deterministic, offline, used by tests.
  - ``PlaywrightFetchProvider`` — local Chromium that visits a URL
    and returns rendered text. Cheap (no LLM tokens) — use for
    competitor scrape / pricing extraction.
  - ``PlaywrightActionProvider`` — local Chromium driven by an LLM
    action loop (click / type / navigate / scroll / done). Handles
    forms, multi-step flows, supervised headed mode. Costs LLM
    tokens per step.
  - ``AgentBrowserCliProvider`` — wraps the npm ``agent-browser`` CLI
    (the one Hermes ships) as a subprocess. Same fetch shape as
    PlaywrightFetchProvider but reuses an already-installed binary
    and emits aria snapshots compatible with Hermes browser skills.
"""
from korpha.browser.providers.agent_browser_cli import AgentBrowserCliProvider
from korpha.browser.providers.mock import MockBrowserProvider
from korpha.browser.providers.playwright_action import PlaywrightActionProvider
from korpha.browser.providers.playwright_fetch import PlaywrightFetchProvider
from korpha.browser.service import (
    BrowserError,
    BrowserProvider,
    BrowserResult,
    BrowserService,
    BrowserTask,
)

__all__ = [
    "AgentBrowserCliProvider",
    "BrowserError",
    "BrowserProvider",
    "BrowserResult",
    "BrowserService",
    "BrowserTask",
    "MockBrowserProvider",
    "PlaywrightActionProvider",
    "PlaywrightFetchProvider",
]
