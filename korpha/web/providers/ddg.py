"""DuckDuckGo — universal free fallback via the ``ddgs`` package.

No API key. Rate-limited by DDG server-side (a few qps per IP). Good
enough for the "no agent without search" baseline. Pair with a keyed
provider higher in the cascade for production scale.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class DDGProvider(WebSearchProvider):
    name = "ddg"
    display_name = "DuckDuckGo"
    requires_key = False

    def is_configured(self) -> bool:
        try:
            import ddgs  # noqa: F401
            return True
        except ImportError:
            return False

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError:
            return []
        q = query if site is None else f"{query} site:{site}"

        def _blocking() -> list[dict[str, Any]]:
            try:
                kw: dict[str, Any] = {"max_results": max_results}
                if recency_days is not None:
                    if recency_days <= 1:
                        kw["timelimit"] = "d"
                    elif recency_days <= 7:
                        kw["timelimit"] = "w"
                    elif recency_days <= 31:
                        kw["timelimit"] = "m"
                    else:
                        kw["timelimit"] = "y"
                return list(DDGS().text(q, **kw))
            except Exception:  # noqa: BLE001
                logger.warning("ddg search failed", exc_info=True)
                return []

        rows = await asyncio.get_event_loop().run_in_executor(None, _blocking)
        return [
            SearchResult(
                title=str(r.get("title") or "").strip(),
                url=str(r.get("href") or r.get("url") or "").strip(),
                snippet=str(r.get("body") or r.get("snippet") or "").strip(),
                provider="ddg",
            )
            for r in rows
            if r.get("href") or r.get("url")
        ]


__all__ = ["DDGProvider"]
