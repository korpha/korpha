"""Common types for web search + extraction providers.

One ``SearchResult`` shape across all 15 providers so the caller doesn't
care which one served the request. Errors are surfaced as empty result
lists + a logged warning — search must never raise, since it runs inside
the agent loop.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchResult:
    """One hit from any provider."""

    title: str
    url: str
    snippet: str
    """Short excerpt — most providers cap at ~300-500 chars."""

    score: float | None = None
    """Optional relevance score (Exa/Perplexity expose one; most don't)."""

    published_at: str | None = None
    """ISO date if the provider returns it; otherwise None."""

    provider: str = ""
    """Which provider served this hit — useful for citation tracking."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras (e.g. Tavily's raw_content, Exa's
    highlights). Don't rely on the shape across providers."""


@dataclass(frozen=True)
class ExtractResult:
    """Full-content extraction of a URL (Firecrawl / Tavily / Exa / etc.)."""

    url: str
    title: str
    content: str
    """Markdown or plain-text body."""

    provider: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class WebSearchProvider(ABC):
    """Single-method contract every provider implements.

    Providers are NEVER allowed to raise from ``search`` / ``extract``;
    log + return ``[]`` on any failure so the cascade can try the next
    one and the agent loop never crashes on search."""

    name: str = "abstract"
    display_name: str = "Abstract"
    requires_key: bool = False
    """If True, ``is_configured()`` checks env vars. Free / local
    providers (DDG, SearXNG) set False."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if this provider has everything it needs to run
        (env var set, lib installed, local URL reachable). Cheap — runs
        on every cascade build, MUST not perform network I/O."""

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        """Run a search. ``site=`` restricts to one domain when supported;
        ``recency_days=`` filters to last N days when supported. Providers
        that don't support these flags ignore them gracefully."""

    async def extract(self, url: str) -> ExtractResult | None:
        """Pull the full content of a URL. Optional — only providers
        that support extraction implement it; the abstract default is
        None so cascade callers can detect 'this provider can't extract'."""
        return None


__all__ = [
    "ExtractResult",
    "SearchResult",
    "WebSearchProvider",
]
