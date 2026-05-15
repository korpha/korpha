"""Parallel.ai — async search + multi-page extract. ``PARALLEL_API_KEY``
required. Sign up at https://parallel.ai.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)

_SEARCH = "https://api.parallel.ai/v1beta/search"


class ParallelProvider(WebSearchProvider):
    name = "parallel"
    display_name = "Parallel.ai"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(os.environ.get("PARALLEL_API_KEY", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = os.environ.get("PARALLEL_API_KEY", "").strip()
        if not key:
            return []
        payload: dict = {
            "objective": query,
            "max_results": max(1, min(max_results, 20)),
        }
        if site:
            payload["objective"] = f"{query} (only sites: {site})"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    _SEARCH, json=payload,
                    headers={"x-api-key": key, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("parallel search failed", exc_info=True)
            return []
        out: list[SearchResult] = []
        for r in (data.get("results") or [])[:max_results]:
            url = r.get("url") or ""
            if not url:
                continue
            out.append(SearchResult(
                title=str(r.get("title") or "").strip(),
                url=url,
                snippet=str(r.get("excerpts", [""])[0] if r.get("excerpts") else "").strip(),
                provider="parallel",
                extra={"excerpts": r.get("excerpts", [])},
            ))
        return out


__all__ = ["ParallelProvider"]
