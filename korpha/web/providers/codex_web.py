"""Codex Responses API — native ``web_search`` tool via OAuth.

Hits ``https://chatgpt.com/backend-api/codex`` with the user's existing
ChatGPT/Codex OAuth token (no separate OPENAI_API_KEY). The native
Responses API ``web_search`` tool returns model-synthesized answer +
citation URLs.

Reads token from ``~/.codex/auth.json`` — managed by ``codex login``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from korpha.web.types import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)

_BASE = "https://chatgpt.com/backend-api/codex"
_AUTH_PATH = Path.home() / ".codex" / "auth.json"


def _read_codex_token() -> str | None:
    if not _AUTH_PATH.exists():
        return None
    try:
        data = json.loads(_AUTH_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    tokens = data.get("tokens") or {}
    tok = tokens.get("access_token")
    if not isinstance(tok, str) or not tok.strip():
        return None
    return tok.strip()


class CodexWebProvider(WebSearchProvider):
    name = "codex"
    display_name = "Codex Responses API (web_search)"
    requires_key = False

    def is_configured(self) -> bool:
        return _read_codex_token() is not None

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        site: str | None = None,
        recency_days: int | None = None,
    ) -> list[SearchResult]:
        token = _read_codex_token()
        if not token:
            return []
        q = query if site is None else f"{query} site:{site}"
        payload = {
            "model": "gpt-5.4",
            "store": False,
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
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{_BASE}/v1/responses",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            # 403 is expected today — chatgpt.com Cloudflare WAF blocks
            # bare httpx calls. The full transport port (with WAF
            # headers + Codex OAuth refresh) is the next architectural
            # PR. Log at DEBUG so the cascade falls through silently.
            logger.debug(
                "codex web_search %s; falling through to next provider",
                exc.response.status_code if exc.response else "error",
            )
            return []
        except Exception:  # noqa: BLE001
            logger.debug("codex web_search failed", exc_info=True)
            return []
        # Responses API: data["output"] is a list of items; we scan for
        # web_search_call results and the message with citations.
        out: list[SearchResult] = []
        answer = ""
        for item in data.get("output") or []:
            t = item.get("type")
            if t == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        answer = (c.get("text") or "").strip()
                        annotations = c.get("annotations") or []
                        for i, ann in enumerate(annotations[:max_results]):
                            if ann.get("type") == "url_citation":
                                url = ann.get("url") or ""
                                if url:
                                    out.append(SearchResult(
                                        title=str(ann.get("title") or f"Source {i+1}").strip(),
                                        url=url,
                                        snippet=answer if i == 0 else "",
                                        provider="codex",
                                    ))
        if not out and answer:
            out.append(SearchResult(
                title="Codex web_search answer", url="",
                snippet=answer, provider="codex",
            ))
        return out


__all__ = ["CodexWebProvider"]
