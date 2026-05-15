"""xAI Grok — Live Search via xAI API. ``XAI_API_KEY`` required.
Includes X / Twitter search (unique to Grok)."""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class GrokProvider(WebSearchProvider):
    name = "grok"
    display_name = "Grok (xAI Live Search)"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(os.environ.get("XAI_API_KEY", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            return []
        sources: list[dict] = [{"type": "web"}, {"type": "x"}, {"type": "news"}]
        if site:
            sources[0]["included_websites"] = [site]
        search_params: dict = {
            "mode": "on",
            "max_search_results": max(1, min(max_results, 20)),
            "sources": sources,
            "return_citations": True,
        }
        if recency_days is not None:
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(days=recency_days)).strftime("%Y-%m-%d")
            search_params["from_date"] = since
        payload = {
            "model": "grok-4",
            "messages": [{"role": "user", "content": query}],
            "search_parameters": search_params,
            "max_tokens": 600,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("grok live search failed", exc_info=True)
            return []
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        answer = msg.get("content") or ""
        citations = data.get("citations") or msg.get("citations") or []
        out: list[SearchResult] = []
        for i, url in enumerate(citations[:max_results]):
            out.append(SearchResult(
                title=f"Source {i + 1}",
                url=str(url).strip(),
                snippet=answer if i == 0 else "",
                provider="grok",
                extra={"synthesized_answer": answer if i == 0 else None},
            ))
        if not out and answer:
            out.append(SearchResult(
                title="Grok synthesized answer", url="",
                snippet=answer, provider="grok",
            ))
        return out


__all__ = ["GrokProvider"]
