"""Claude native ``web_search_20250305`` tool via Anthropic API.

Requires ``ANTHROPIC_API_KEY``. Returns model-synthesized answer with
citation URLs from Claude's built-in web search tool.
"""
from __future__ import annotations

import logging
import os

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)


class ClaudeWebProvider(WebSearchProvider):
    name = "claude"
    display_name = "Claude native web_search"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return []
        tool: dict = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max(1, min(max_results, 10)),
        }
        if site:
            tool["allowed_domains"] = [site]
        q = query
        payload = {
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": q}],
            "tools": [tool],
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("claude web_search failed", exc_info=True)
            return []
        out: list[SearchResult] = []
        answer = ""
        for block in data.get("content") or []:
            t = block.get("type")
            if t == "text":
                answer += block.get("text", "")
            elif t == "web_search_tool_result":
                for src in block.get("content") or []:
                    if src.get("type") != "web_search_result":
                        continue
                    url = src.get("url") or ""
                    if not url:
                        continue
                    out.append(SearchResult(
                        title=str(src.get("title") or "").strip(),
                        url=url,
                        snippet=str(src.get("page_age") or "").strip(),
                        published_at=src.get("page_age"),
                        provider="claude",
                    ))
        if out and answer:
            # Stamp the synthesized answer on the first hit.
            first = out[0]
            out[0] = SearchResult(
                title=first.title, url=first.url,
                snippet=answer.strip()[:600] or first.snippet,
                score=first.score, published_at=first.published_at,
                provider=first.provider, extra={"synthesized_answer": answer.strip()},
            )
        elif answer:
            out.append(SearchResult(
                title="Claude synthesized answer", url="",
                snippet=answer.strip(), provider="claude",
            ))
        return out[:max_results]


__all__ = ["ClaudeWebProvider"]
