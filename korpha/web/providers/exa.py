"""Exa — neural search + content extraction. ``EXA_API_KEY`` required.

Sign up at https://exa.ai.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from korpha.web.types import ExtractResult, SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)

_SEARCH = "https://api.exa.ai/search"
_CONTENTS = "https://api.exa.ai/contents"


class ExaProvider(WebSearchProvider):
    name = "exa"
    display_name = "Exa"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(os.environ.get("EXA_API_KEY", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = os.environ.get("EXA_API_KEY", "").strip()
        if not key:
            return []
        payload: dict = {
            "query": query,
            "numResults": max(1, min(max_results, 25)),
            "type": "neural",
            "contents": {
                "text": {"maxCharacters": 600},
                "highlights": True,
            },
        }
        if site:
            payload["includeDomains"] = [site]
        if recency_days is not None:
            since = (datetime.now(timezone.utc) - timedelta(days=recency_days)).strftime("%Y-%m-%d")
            payload["startPublishedDate"] = since
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _SEARCH,
                    json=payload,
                    headers={"x-api-key": key},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("exa search failed", exc_info=True)
            return []
        out: list[SearchResult] = []
        for r in (data.get("results") or [])[:max_results]:
            url = r.get("url") or ""
            if not url:
                continue
            text = r.get("text") or ""
            highlights = r.get("highlights") or []
            snippet = " ".join(highlights[:3]) if highlights else text[:500]
            out.append(SearchResult(
                title=str(r.get("title") or "").strip(),
                url=url,
                snippet=str(snippet).strip(),
                score=r.get("score"),
                published_at=r.get("publishedDate"),
                provider="exa",
                extra={"author": r.get("author")},
            ))
        return out

    async def extract(self, url: str) -> ExtractResult | None:
        key = os.environ.get("EXA_API_KEY", "").strip()
        if not key:
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _CONTENTS,
                    json={"ids": [url], "text": True},
                    headers={"x-api-key": key},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("exa extract failed", exc_info=True)
            return None
        items = data.get("results") or []
        if not items:
            return None
        r = items[0]
        return ExtractResult(
            url=r.get("url") or url,
            title=str(r.get("title") or "").strip(),
            content=str(r.get("text") or "").strip(),
            provider="exa",
        )


__all__ = ["ExaProvider"]
