"""Ollama Cloud — Web Search API.

``ollama serve`` does not include search; the cloud product does:
https://docs.ollama.com/cloud/web-search. Auth via ``OLLAMA_API_KEY``
header (or anonymous when self-hosted with no auth)."""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class OllamaWebProvider(WebSearchProvider):
    name = "ollama"
    display_name = "Ollama Cloud Web Search"
    requires_key = False

    def is_configured(self) -> bool:
        # OLLAMA_WEB_URL gates this provider explicitly; we don't want to
        # accidentally hit Ollama's cloud when the user only has a local
        # instance for inference.
        return bool(os.environ.get("OLLAMA_WEB_URL", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        base = os.environ.get(
            "OLLAMA_WEB_URL", "https://ollama.com/api/web_search",
        ).strip().rstrip("/")
        key = os.environ.get("OLLAMA_API_KEY", "").strip()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        q = query if site is None else f"{query} site:{site}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    base,
                    json={"query": q, "max_results": max(1, min(max_results, 20))},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("ollama web search failed", exc_info=True)
            return []
        out: list[SearchResult] = []
        for r in (data.get("results") or [])[:max_results]:
            url = r.get("url") or r.get("link") or ""
            if not url:
                continue
            out.append(SearchResult(
                title=str(r.get("title") or "").strip(),
                url=url,
                snippet=str(r.get("content") or r.get("snippet") or "").strip(),
                provider="ollama",
            ))
        return out


__all__ = ["OllamaWebProvider"]
