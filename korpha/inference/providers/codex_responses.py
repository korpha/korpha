"""Codex Responses API provider — replaces ``codex exec`` subprocess.

Direct HTTP to ``chatgpt.com/backend-api/codex/responses`` via the
user's existing Codex OAuth (same auth ``codex login`` manages, same
auth the codex CLI uses). Faster than spawning ``codex exec`` per call
and unlocks the native tool surface (web_search, image_generation)
when we wire tools in.

Used by the ``codex-cli`` provider preset when codex CLI ≥ 0.125 is
detected — the legacy subprocess path stays as a fallback for older
codex installs and as a backstop when the Cloudflare WAF rejects us.

Hermes uses the OpenAI SDK with ``client.responses.stream`` for this
surface; we go raw httpx + SSE so we don't depend on the openai
package and can pin the WAF headers precisely (see
:mod:`korpha.inference.codex_oauth`).
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from korpha.inference.codex_oauth import (
    CodexAuthError,
    cloudflare_headers,
    get_codex_auth,
)
from korpha.inference.provider import Provider, ProviderError
from korpha.inference.registry import ProviderAccount
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    Role,
    StreamChunk,
)

logger = logging.getLogger(__name__)

_BASE = "https://chatgpt.com/backend-api/codex"


@dataclass
class CodexResponsesProvider(Provider):
    """OAuth-authed HTTP client for the Codex Responses surface."""

    name: str = "codex-cli"
    """Same name as the subprocess provider so existing
    ``preset: codex-cli`` entries in providers.yaml work unchanged."""

    timeout_seconds: float = 180.0

    reasoning_effort: str | None = None
    """Optional ``reasoning.effort`` value to send to the Responses
    API — one of ``low`` / ``medium`` / ``high`` / ``xhigh`` / ``max``.
    ``None`` (default) lets the subscription pick its own heuristic;
    set explicitly when you want the model to think harder than the
    subscription's auto-routing would choose."""

    async def complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> CompletionResponse:
        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"Codex Responses account {account.label or account.id} "
                f"has no model mapped for tier {request.tier!s}"
            )
        try:
            auth = get_codex_auth()
        except CodexAuthError as exc:
            raise ProviderError(f"codex auth: {exc}") from exc

        instructions, user_input = _split_system_from_messages(request.messages)
        payload: dict[str, Any] = {
            "model": model or "gpt-5.4",
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": user_input,
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        # Note: ``chatgpt.com/backend-api/codex/responses`` rejects
        # ``max_output_tokens`` (returns 400 "Unsupported parameter").
        # The subscription routes inside Codex pick limits automatically.
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **cloudflare_headers(auth.access_token),
        }

        answer = ""
        reasoning = ""
        finish: str = "stop"
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    request.timeout_seconds or self.timeout_seconds,
                    connect=20.0,
                ),
            ) as client:
                async with client.stream(
                    "POST", f"{_BASE}/responses",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise ProviderError(
                            f"Codex Responses {resp.status_code}: "
                            + body.decode("utf-8", errors="replace")[:400]
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        t = event.get("type", "")
                        if t == "response.output_text.delta":
                            answer += event.get("delta", "")
                        elif t == "response.reasoning_summary_text.delta":
                            reasoning += event.get("delta", "")
                        elif t == "response.completed":
                            response = event.get("response") or {}
                            usage = response.get("usage") or {}
                            input_tokens = int(usage.get("input_tokens") or 0)
                            output_tokens = int(usage.get("output_tokens") or 0)
                            details = usage.get("input_tokens_details") or {}
                            cached_tokens = int(details.get("cached_tokens") or 0)
                            status = response.get("status") or "completed"
                            if status == "incomplete":
                                reason = (
                                    response.get("incomplete_details") or {}
                                ).get("reason")
                                if reason == "max_output_tokens":
                                    finish = "length"
                                else:
                                    finish = "error"
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Codex Responses transport: {type(exc).__name__}: {exc}"
            ) from exc

        if not answer.strip():
            raise ProviderError(
                "Codex Responses returned no text content"
            )

        return CompletionResponse(
            content=answer,
            tool_calls=(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=Decimal("0"),  # subscription-paid
            provider=self.name,
            model=model or "gpt-5.4",
            account_id=str(account.id),
            reasoning=reasoning or None,
            finish_reason=finish,
            cache_hit_ratio=(cached_tokens / max(input_tokens, 1)),
        )

    async def stream_complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> AsyncIterator[StreamChunk]:
        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"Codex Responses account {account.label or account.id} "
                f"has no model mapped for tier {request.tier!s}"
            )
        try:
            auth = get_codex_auth()
        except CodexAuthError as exc:
            raise ProviderError(f"codex auth: {exc}") from exc

        instructions, user_input = _split_system_from_messages(request.messages)
        payload: dict[str, Any] = {
            "model": model or "gpt-5.4",
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": user_input,
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **cloudflare_headers(auth.access_token),
        }

        finish_reason: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    request.timeout_seconds or self.timeout_seconds,
                    connect=20.0,
                ),
            ) as client:
                async with client.stream(
                    "POST", f"{_BASE}/responses",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise ProviderError(
                            f"Codex Responses {resp.status_code}: "
                            + body.decode("utf-8", errors="replace")[:400]
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        t = event.get("type", "")
                        if t == "response.output_text.delta":
                            delta = event.get("delta", "")
                            if delta:
                                yield StreamChunk(delta_content=delta, raw=event)
                        elif t == "response.reasoning_summary_text.delta":
                            delta = event.get("delta", "")
                            if delta:
                                yield StreamChunk(delta_reasoning=delta, raw=event)
                        elif t == "response.completed":
                            response = event.get("response") or {}
                            status = response.get("status") or "completed"
                            if status == "incomplete":
                                reason = (
                                    response.get("incomplete_details") or {}
                                ).get("reason")
                                finish_reason = (
                                    "length" if reason == "max_output_tokens"
                                    else "error"
                                )
                            else:
                                finish_reason = "stop"
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Codex Responses transport: {type(exc).__name__}: {exc}"
            ) from exc

        yield StreamChunk(finish_reason=finish_reason or "stop")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _split_system_from_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, Any]]]:
    """Codex Responses takes ``instructions`` (system prompt) +
    ``input`` (user-/assistant-shaped messages) — separate fields.
    Flatten our Message list into that shape."""
    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for m in messages:
        if not m.content:
            continue
        if m.role == Role.SYSTEM:
            instructions_parts.append(m.content)
        else:
            input_items.append({
                "type": "message",
                "role": m.role.value if hasattr(m.role, "value") else str(m.role),
                "content": [{
                    "type": (
                        "input_text" if m.role == Role.USER
                        else "output_text"
                    ),
                    "text": m.content,
                }],
            })
    instructions = "\n\n".join(instructions_parts) if instructions_parts else (
        "You are a helpful assistant."
    )
    if not input_items:
        # Codex Responses requires non-empty input; synthesize a no-op.
        input_items.append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "(begin)"}],
        })
    return instructions, input_items


__all__ = ["CodexResponsesProvider"]
