"""Firecrawl — search + deep extraction. ``FIRECRAWL_API_KEY`` required
(or ``FIRECRAWL_API_URL`` for self-hosted).
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import ExtractResult, SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


def _base_url() -> str:
    custom = os.environ.get("FIRECRAWL_API_URL", "").strip()
    return custom.rstrip("/") if custom else "https://api.firecrawl.dev"


class FirecrawlProvider(WebSearchProvider):
    name = "firecrawl"
    display_name = "Firecrawl"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("FIRECRAWL_API_KEY", "").strip()
            or os.environ.get("FIRECRAWL_API_URL", "").strip()
        )

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        q = query if site is None else f"{query} site:{site}"
        payload: dict = {
            "query": q,
            "limit": max(1, min(max_results, 20)),
            "scrapeOptions": {"formats": ["markdown"]},
        }
        if recency_days is not None:
            if recency_days <= 1:
                payload["tbs"] = "qdr:d"
            elif recency_days <= 7:
                payload["tbs"] = "qdr:w"
            elif recency_days <= 31:
                payload["tbs"] = "qdr:m"
            else:
                payload["tbs"] = "qdr:y"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{_base_url()}/v1/search", json=payload, headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("firecrawl search failed", exc_info=True)
            return []
        out: list[SearchResult] = []
        for r in (data.get("data") or [])[:max_results]:
            url = r.get("url") or ""
            if not url:
                continue
            out.append(SearchResult(
                title=str(r.get("title") or "").strip(),
                url=url,
                snippet=str(r.get("description") or "").strip(),
                provider="firecrawl",
                extra={"markdown": r.get("markdown")},
            ))
        return out

    async def extract(self, url: str) -> ExtractResult | None:
        key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{_base_url()}/v1/scrape",
                    json={"url": url, "formats": ["markdown"]},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("firecrawl extract failed", exc_info=True)
            return None
        d = data.get("data") or {}
        markdown = d.get("markdown") or ""
        meta = d.get("metadata") or {}
        if not markdown:
            return None
        return ExtractResult(
            url=meta.get("sourceURL") or url,
            title=str(meta.get("title") or "").strip(),
            content=markdown.strip(),
            provider="firecrawl",
        )


__all__ = ["FirecrawlProvider"]
