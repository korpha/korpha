"""Perplexity Sonar — AI-synthesized answers with citations. Either
``PERPLEXITY_API_KEY`` direct or ``OPENROUTER_API_KEY`` via OpenRouter.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class PerplexityProvider(WebSearchProvider):
    name = "perplexity"
    display_name = "Perplexity Sonar"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("PERPLEXITY_API_KEY", "").strip()
            or os.environ.get("OPENROUTER_API_KEY", "").strip()
        )

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        ppl_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
        or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not ppl_key and not or_key:
            return []
        if ppl_key:
            url = "https://api.perplexity.ai/chat/completions"
            headers = {"Authorization": f"Bearer {ppl_key}"}
            model = "sonar-pro"
        else:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {or_key}"}
            model = "perplexity/sonar-pro"
        q = query if site is None else f"{query} site:{site}"
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": q}],
            "max_tokens": 600,
        }
        if recency_days is not None:
            if recency_days <= 1:
                payload["search_recency_filter"] = "day"
            elif recency_days <= 7:
                payload["search_recency_filter"] = "week"
            elif recency_days <= 31:
                payload["search_recency_filter"] = "month"
            else:
                payload["search_recency_filter"] = "year"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("perplexity search failed", exc_info=True)
            return []
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        answer = msg.get("content") or ""
        citations = data.get("citations") or []
        out: list[SearchResult] = []
        # Perplexity returns ONE synthesized answer + a flat list of source URLs.
        # Map each citation URL to a SearchResult; first carries the answer.
        for i, cite_url in enumerate(citations[:max_results]):
            out.append(SearchResult(
                title=f"Source {i + 1}",
                url=str(cite_url).strip(),
                snippet=answer if i == 0 else "",
                provider="perplexity",
                extra={"synthesized_answer": answer if i == 0 else None},
            ))
        if not out and answer:
            # No citations but we have an answer — surface it as a single hit.
            out.append(SearchResult(
                title="Perplexity synthesized answer",
                url="",
                snippet=answer,
                provider="perplexity",
            ))
        return out


__all__ = ["PerplexityProvider"]
