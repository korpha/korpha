"""Codex Responses API — native ``web_search`` tool via OAuth.

Hits ``https://chatgpt.com/backend-api/codex`` with the user's existing
ChatGPT/Codex OAuth token (no separate OPENAI_API_KEY). The native
Responses API ``web_search`` tool returns model-synthesized answer +
citation URLs.

Routes via :mod:`korpha.inference.codex_oauth` for token + Cloudflare
WAF headers (the bare-httpx 403 fix).
"""
from __future__ import annotations

import logging

import httpx

from korpha.inference.codex_oauth import (
    CodexAuthError,
    cloudflare_headers,
    get_codex_auth,
    is_configured as codex_is_configured,
)
from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)

_BASE = "https://chatgpt.com/backend-api/codex"


class CodexWebProvider(WebSearchProvider):
    name = "codex"
    display_name = "Codex Responses API (web_search)"
    requires_key = False

    def is_configured(self) -> bool:
        return codex_is_configured()

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        try:
            auth = get_codex_auth()
        except CodexAuthError as exc:
            logger.debug("codex auth unavailable: %s", exc)
            return []
        q = query if site is None else f"{query} site:{site}"
        instructions = (
            "You are a research assistant. Use the web_search tool to "
            "answer the user's query. Return a concise answer that "
            "cites the URLs you used."
        )
        payload = {
            "model": "gpt-5.4",
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": q}],
            }],
            "tools": [{"type": "web_search"}],
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "required",
                "tools": [{"type": "web_search"}],
            },
        }
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **cloudflare_headers(auth.access_token),
        }
        answer = ""
        structured_citations: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
                async with client.stream(
                    "POST", f"{_BASE}/responses",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.warning(
                            "codex web_search %s: %s",
                            resp.status_code,
                            body.decode("utf-8", errors="replace")[:300],
                        )
                        return []
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            import json as _json
                            event = _json.loads(data_str)
                        except Exception:  # noqa: BLE001
                            continue
                        t = event.get("type", "")
                        if t == "response.output_text.delta":
                            answer += event.get("delta", "")
                        elif t == "response.output_item.done":
                            item = event.get("item") or {}
                            if item.get("type") == "message":
                                for c in item.get("content") or []:
                                    for ann in c.get("annotations") or []:
                                        if ann.get("type") == "url_citation":
                                            structured_citations.append(ann)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "codex web_search %s: %s",
                exc.response.status_code if exc.response else "error",
                (exc.response.text[:200] if exc.response else "")
                if exc.response else "",
            )
            return []
        except Exception:  # noqa: BLE001
            logger.debug("codex web_search failed", exc_info=True)
            return []

        answer = answer.strip()
        # Codex sometimes returns structured url_citation annotations and
        # sometimes embeds URLs inline. Pick whichever we got.
        out: list[SearchResult] = []
        if structured_citations:
            for i, ann in enumerate(structured_citations[:max_results]):
                url = ann.get("url") or ""
                if not url:
                    continue
                out.append(SearchResult(
                    title=str(ann.get("title") or f"Source {i+1}").strip(),
                    url=url,
                    snippet=answer if i == 0 else "",
                    provider="codex",
                ))
        else:
            # Regex-extract URLs from the markdown answer. First hit
            # carries the full answer as snippet; later hits are bare
            # citations so the caller can render a source list.
            import re
            url_re = re.compile(r"https?://[^\s)>\]]+")
            seen: set[str] = set()
            urls: list[str] = []
            for m in url_re.finditer(answer):
                u = m.group(0).rstrip(".,;:)>")
                if u in seen:
                    continue
                seen.add(u)
                urls.append(u)
                if len(urls) >= max_results:
                    break
            for i, u in enumerate(urls):
                out.append(SearchResult(
                    title=f"Source {i + 1}",
                    url=u,
                    snippet=answer if i == 0 else "",
                    provider="codex",
                ))
        if not out and answer:
            out.append(SearchResult(
                title="Codex web_search answer", url="",
                snippet=answer, provider="codex",
                extra={"synthesized_answer": answer},
            ))
        return out


__all__ = ["CodexWebProvider"]
