"""Gemini grounding — Google's native search-grounded answers.
Requires ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class GeminiProvider(WebSearchProvider):
    name = "gemini"
    display_name = "Gemini (Google grounding)"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
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
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        if not key:
            return []
        model = os.environ.get("GEMINI_GROUNDING_MODEL", "gemini-2.5-flash").strip()
        q = query if site is None else f"{query} site:{site}"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:"
            f"generateContent?key={key}"
        )
        payload = {
            "contents": [{"parts": [{"text": q}]}],
            "tools": [{"googleSearch": {}}],
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("gemini grounding failed", exc_info=True)
            return []
        candidates = data.get("candidates") or []
        if not candidates:
            return []
        cand = candidates[0]
        content = cand.get("content") or {}
        parts = content.get("parts") or []
        answer = " ".join(p.get("text", "") for p in parts).strip()
        # Grounding metadata holds the source URLs/snippets.
        meta = cand.get("groundingMetadata") or {}
        chunks = meta.get("groundingChunks") or []
        out: list[SearchResult] = []
        for i, c in enumerate(chunks[:max_results]):
            web = c.get("web") or {}
            url_ = web.get("uri") or ""
            title = web.get("title") or f"Source {i + 1}"
            if not url_:
                continue
            out.append(SearchResult(
                title=str(title).strip(),
                url=url_,
                snippet=answer if i == 0 else "",
                provider="gemini",
                extra={"synthesized_answer": answer if i == 0 else None},
            ))
        if not out and answer:
            out.append(SearchResult(
                title="Gemini grounded answer", url="",
                snippet=answer, provider="gemini",
            ))
        return out


__all__ = ["GeminiProvider"]
