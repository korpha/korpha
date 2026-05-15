"""Tavily — search + extract + crawl. ``TAVILY_API_KEY`` required.

Sign up at https://app.tavily.com/home.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import ExtractResult, SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)

_SEARCH = "https://api.tavily.com/search"
_EXTRACT = "https://api.tavily.com/extract"


class TavilyProvider(WebSearchProvider):
    name = "tavily"
    display_name = "Tavily"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(os.environ.get("TAVILY_API_KEY", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not key:
            return []
        payload: dict = {
            "query": query,
            "max_results": max(1, min(max_results, 20)),
            "search_depth": "advanced",
            "include_answer": False,
        }
        if site:
            payload["include_domains"] = [site]
        if recency_days is not None:
            payload["days"] = max(1, recency_days)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _SEARCH,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("tavily search failed", exc_info=True)
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
                score=r.get("score"),
                published_at=r.get("published_date"),
                provider="tavily",
                extra={"raw_content": r.get("raw_content")},
            ))
        return out

    async def extract(self, url: str) -> ExtractResult | None:
        key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not key:
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _EXTRACT,
                    json={"urls": [url], "extract_depth": "advanced"},
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("tavily extract failed", exc_info=True)
            return None
        items = data.get("results") or []
        if not items:
            return None
        r = items[0]
        return ExtractResult(
            url=r.get("url") or url,
            title=str(r.get("title") or "").strip(),
            content=str(r.get("raw_content") or r.get("content") or "").strip(),
            provider="tavily",
        )


__all__ = ["TavilyProvider"]
