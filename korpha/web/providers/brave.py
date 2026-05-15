"""Brave Search free-tier API.

Sign up at https://brave.com/search/api/ for ``BRAVE_SEARCH_API_KEY``.
Free tier: 2,000 queries/month at 1 qps.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveProvider(WebSearchProvider):
    name = "brave"
    display_name = "Brave Search (free tier)"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(os.environ.get("BRAVE_SEARCH_API_KEY", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        if not key:
            return []
        q = query if site is None else f"{query} site:{site}"
        params = {
            "q": q,
            "count": str(max(1, min(max_results, 20))),
        }
        if recency_days is not None:
            if recency_days <= 1:
                params["freshness"] = "pd"
            elif recency_days <= 7:
                params["freshness"] = "pw"
            elif recency_days <= 31:
                params["freshness"] = "pm"
            else:
                params["freshness"] = "py"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": key,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    _ENDPOINT, params=params, headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("brave search failed", exc_info=True)
            return []
        web = (data.get("web") or {}).get("results") or []
        out: list[SearchResult] = []
        for r in web[:max_results]:
            url = r.get("url") or ""
            if not url:
                continue
            out.append(SearchResult(
                title=str(r.get("title") or "").strip(),
                url=url,
                snippet=str(r.get("description") or "").strip(),
                provider="brave",
                published_at=r.get("page_age"),
            ))
        return out


__all__ = ["BraveProvider"]
