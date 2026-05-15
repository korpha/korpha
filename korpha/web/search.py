"""Top-level ``web_search`` API + provider cascade.

Picks the right provider based on what's configured. Order:

1. **Paid model-native + quality** (when keys set): Tavily, Exa,
   Firecrawl, Perplexity, Gemini, Parallel — these are quality-first.
2. **Free with key**: Brave free tier.
3. **Self-hosted free**: SearXNG (URL config), Ollama (local agent).
4. **Always-on free**: DDG via ``ddgs`` package — universal fallback.

Order is configurable via ``WEB_SEARCH_ORDER`` env var (comma-separated
provider names). Default order is in :data:`_DEFAULT_ORDER`.

The cascade tries each configured provider in order; first non-empty
result list wins. On error the provider returns ``[]`` (never raises),
so the cascade moves to the next one transparently.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from korpha.web.types import (
    ExtractResult,
    SearchResult,
    WebSearchProvider,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Default cascade order. Paid+quality first, free+always-on last so the
# best signal wins when keys are present and DDG catches everything else.
_DEFAULT_ORDER: tuple[str, ...] = (
    "perplexity",       # AI-synthesized answers w/ citations
    "tavily",           # search+extract, good for research
    "exa",              # neural search w/ semantic matching
    "firecrawl",        # deep extract
    "parallel",         # async multi-page
    "gemini",           # Google search grounding
    "grok",             # X / Twitter
    "kimi",             # AI-synth + citations
    "minimax",          # structured search
    "brave",            # free tier 2k/mo
    "searxng",          # self-hosted meta-search
    "ollama",           # local search agent
    "codex",            # Codex Responses API web_search tool
    "claude",           # Claude SDK web_search
    "ddg",              # universal fallback — no key, free
)


def _build_provider(name: str) -> WebSearchProvider | None:
    """Lazy-build a provider by name. Returns None when the provider
    isn't configured (env var missing, lib not installed, etc.)."""
    name = name.strip().lower()
    try:
        if name == "ddg" or name == "ddgs" or name == "duckduckgo":
            from korpha.web.providers.ddg import DDGProvider
            p = DDGProvider()
        elif name == "brave" or name == "brave-free":
            from korpha.web.providers.brave import BraveProvider
            p = BraveProvider()
        elif name == "tavily":
            from korpha.web.providers.tavily import TavilyProvider
            p = TavilyProvider()
        elif name == "exa":
            from korpha.web.providers.exa import ExaProvider
            p = ExaProvider()
        elif name == "firecrawl":
            from korpha.web.providers.firecrawl import FirecrawlProvider
            p = FirecrawlProvider()
        elif name == "parallel":
            from korpha.web.providers.parallel import ParallelProvider
            p = ParallelProvider()
        elif name == "perplexity":
            from korpha.web.providers.perplexity import PerplexityProvider
            p = PerplexityProvider()
        elif name == "gemini":
            from korpha.web.providers.gemini import GeminiProvider
            p = GeminiProvider()
        elif name == "grok":
            from korpha.web.providers.grok import GrokProvider
            p = GrokProvider()
        elif name == "kimi":
            from korpha.web.providers.kimi import KimiProvider
            p = KimiProvider()
        elif name == "minimax":
            from korpha.web.providers.minimax import MiniMaxProvider
            p = MiniMaxProvider()
        elif name == "searxng":
            from korpha.web.providers.searxng import SearXNGProvider
            p = SearXNGProvider()
        elif name == "ollama":
            from korpha.web.providers.ollama_web import OllamaWebProvider
            p = OllamaWebProvider()
        elif name == "codex":
            from korpha.web.providers.codex_web import CodexWebProvider
            p = CodexWebProvider()
        elif name == "claude":
            from korpha.web.providers.claude_web import ClaudeWebProvider
            p = ClaudeWebProvider()
        else:
            logger.warning("web.search: unknown provider %r", name)
            return None
    except ImportError as exc:
        logger.debug("web.search: provider %s not importable: %s", name, exc)
        return None
    except Exception:  # noqa: BLE001
        logger.warning(
            "web.search: provider %s failed to build", name, exc_info=True,
        )
        return None
    return p


def _configured_order() -> tuple[str, ...]:
    """Returns the cascade order respecting ``WEB_SEARCH_ORDER`` env."""
    override = os.environ.get("WEB_SEARCH_ORDER", "").strip()
    if override:
        parts = tuple(p.strip().lower() for p in override.split(",") if p.strip())
        return parts
    return _DEFAULT_ORDER


def list_available() -> list[tuple[str, bool]]:
    """Return ``[(provider_name, is_configured), ...]`` for the
    configured cascade order. Used by ``korpha web status`` + dashboard."""
    out: list[tuple[str, bool]] = []
    for name in _configured_order():
        p = _build_provider(name)
        if p is None:
            out.append((name, False))
        else:
            try:
                out.append((name, p.is_configured()))
            except Exception:  # noqa: BLE001
                out.append((name, False))
    return out


async def web_search(
    query: str,
    *,
    max_results: int = 5,
    site: str | None = None,
    recency_days: int | None = None,
) -> list[SearchResult]:
    """Search the web via the configured cascade. Returns the first
    non-empty result list; empty when nothing matched and no provider
    succeeded."""
    if not query or not query.strip():
        return []

    last_err: str | None = None
    for name in _configured_order():
        p = _build_provider(name)
        if p is None:
            continue
        try:
            if not p.is_configured():
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            results = await p.search(
                query.strip(),
                max_results=max_results,
                site=site,
                recency_days=recency_days,
            )
        except Exception as exc:  # noqa: BLE001
            last_err = f"{name}: {type(exc).__name__}: {exc}"
            logger.warning(
                "web.search: provider %s raised, falling through", name,
                exc_info=True,
            )
            continue
        if results:
            logger.info(
                "web.search: %s returned %d result(s) for %r",
                name, len(results), query[:80],
            )
            return results

    if last_err:
        logger.warning("web.search: all providers exhausted; last err: %s", last_err)
    else:
        logger.info("web.search: no provider returned results for %r", query[:80])
    return []


async def web_extract(url: str) -> ExtractResult | None:
    """Pull full content for a URL. Tries providers that support
    extract() in cascade order; returns the first non-None result."""
    if not url or not url.strip():
        return None
    for name in _configured_order():
        p = _build_provider(name)
        if p is None:
            continue
        try:
            if not p.is_configured():
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            extracted = await p.extract(url.strip())
        except Exception:  # noqa: BLE001
            logger.warning(
                "web.extract: provider %s raised", name, exc_info=True,
            )
            continue
        if extracted is not None:
            return extracted
    return None


__all__ = [
    "ExtractResult",
    "SearchResult",
    "list_available",
    "web_extract",
    "web_search",
]
