"""SearXNG — self-hosted privacy-respecting meta-search.
Requires ``SEARXNG_URL`` pointing at your instance.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class SearXNGProvider(WebSearchProvider):
    name = "searxng"
    display_name = "SearXNG (self-hosted)"
    requires_key = False

    def is_configured(self) -> bool:
        return bool(os.environ.get("SEARXNG_URL", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        base = os.environ.get("SEARXNG_URL", "").strip().rstrip("/")
        if not base:
            return []
        q = query if site is None else f"{query} site:{site}"
        params = {
            "q": q,
            "format": "json",
        }
        if recency_days is not None:
            if recency_days <= 1:
                params["time_range"] = "day"
            elif recency_days <= 31:
                params["time_range"] = "month"
            else:
                params["time_range"] = "year"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{base}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("searxng search failed", exc_info=True)
            return []
        out: list[SearchResult] = []
        for r in (data.get("results") or [])[:max_results]:
            url = r.get("url") or ""
            if not url:
                continue
            out.append(SearchResult(
                title=str(r.get("title") or "").strip(),
                url=url,
                snippet=str(r.get("content") or "").strip(),
                published_at=r.get("publishedDate"),
                provider="searxng",
            ))
        return out


__all__ = ["SearXNGProvider"]
