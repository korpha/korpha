"""MiniMax — built-in ``web_search`` tool via the Code Plan API.
``MINIMAX_CODE_PLAN_KEY`` (or ``MINIMAX_API_KEY``) required.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class MiniMaxProvider(WebSearchProvider):
    name = "minimax"
    display_name = "MiniMax"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("MINIMAX_CODE_PLAN_KEY", "").strip()
            or os.environ.get("MINIMAX_API_KEY", "").strip()
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
            os.environ.get("MINIMAX_CODE_PLAN_KEY", "").strip()
            or os.environ.get("MINIMAX_API_KEY", "").strip()
        )
        if not key:
            return []
        q = query if site is None else f"{query} site:{site}"
        payload = {
            "model": "MiniMax-M2",
            "messages": [{"role": "user", "content": q}],
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "max_tokens": 800,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.minimaxi.com/v1/text/chatcompletion_v2",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("minimax search failed", exc_info=True)
            return []
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        answer = msg.get("content") or ""
        meta = msg.get("web_search") or {}
        sources = meta.get("results") or []
        out: list[SearchResult] = []
        for i, src in enumerate(sources[:max_results]):
            url = src.get("link") or src.get("url") or ""
            if not url:
                continue
            out.append(SearchResult(
                title=str(src.get("title") or f"Source {i + 1}").strip(),
                url=url,
                snippet=str(src.get("snippet") or "").strip() or (answer if i == 0 else ""),
                provider="minimax",
            ))
        if not out and answer:
            out.append(SearchResult(
                title="MiniMax synthesized answer", url="",
                snippet=answer, provider="minimax",
            ))
        return out


__all__ = ["MiniMaxProvider"]
