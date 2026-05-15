"""Kimi (Moonshot) — built-in ``$web_search`` tool. ``KIMI_API_KEY``
or ``MOONSHOT_API_KEY``."""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class KimiProvider(WebSearchProvider):
    name = "kimi"
    display_name = "Kimi (Moonshot)"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("KIMI_API_KEY", "").strip()
            or os.environ.get("MOONSHOT_API_KEY", "").strip()
        )

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = (
            os.environ.get("KIMI_API_KEY", "").strip()
            or os.environ.get("MOONSHOT_API_KEY", "").strip()
        )
        if not key:
            return []
        q = query if site is None else f"{query} site:{site}"
        payload = {
            "model": "kimi-k2-0905-preview",
            "messages": [{"role": "user", "content": q}],
            "tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}],
            "max_tokens": 800,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.moonshot.ai/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("kimi search failed", exc_info=True)
            return []
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        answer = msg.get("content") or ""
        # Kimi puts tool results in tool_calls / search_results metadata.
        meta = msg.get("metadata") or {}
        citations = meta.get("search_results") or []
        out: list[SearchResult] = []
        for i, src in enumerate(citations[:max_results]):
            url = src.get("url") or src.get("link") or ""
            if not url:
                continue
            out.append(SearchResult(
                title=str(src.get("title") or f"Source {i + 1}").strip(),
                url=url,
                snippet=str(src.get("snippet") or "").strip() or (answer if i == 0 else ""),
                provider="kimi",
            ))
        if not out and answer:
            out.append(SearchResult(
                title="Kimi synthesized answer", url="",
                snippet=answer, provider="kimi",
            ))
        return out


__all__ = ["KimiProvider"]
