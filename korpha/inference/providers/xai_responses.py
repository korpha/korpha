"""xAI Grok Responses API provider — speaks the Responses surface
at ``https://api.x.ai/v1/responses`` using a SuperGrok subscription
bearer (no API key required).

Models that the X Premium+ / SuperGrok subscription gates access to:
  - ``grok-4.3``                              (default chat/general)
  - ``grok-4.20-0309-reasoning``              (deep reasoning)
  - ``grok-4.20-0309-non-reasoning``          (fast, no CoT)
  - ``grok-4.20-multi-agent-0309``            (multi-agent)
  - Image / video generation via ``grok-imagine-*`` (separate flow).

The transport mirrors :mod:`korpha.inference.providers.codex_responses`
because both speak the OpenAI-style Responses streaming protocol —
the only delta is the auth source (xAI OAuth bearer instead of Codex
OAuth + Cloudflare WAF headers).
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from korpha.inference.provider import Provider, ProviderError
from korpha.inference.providers.codex_responses import _split_system_from_messages
from korpha.inference.registry import ProviderAccount
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from korpha.inference.xai_oauth import (
    XAI_API_BASE,
    XaiOAuthError,
    get_auth,
)

logger = logging.getLogger(__name__)


def _business_unit_id_from_request(
    request: CompletionRequest,
) -> str | None:
    """Pull business_unit_id off the request when present so per-unit
    subscriptions get used. Falls back to the install-wide token."""
    # The request may carry a ``metadata`` dict for downstream
    # routing — older callers won't, which is fine (None = install-wide).
    metadata = getattr(request, "metadata", None) or {}
    bid = metadata.get("business_unit_id")
    return str(bid) if bid else None


@dataclass
class XaiResponsesProvider(Provider):
    """OAuth-authed HTTP client for the xAI Responses surface."""

    name: str = "xai-oauth"
    """Matches the provider preset registered in
    :mod:`korpha.inference.providers.builtins`."""

    timeout_seconds: float = 180.0

    async def complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> CompletionResponse:
        model = account.tier_models.get(request.tier)
        if not model:
            raise ProviderError(
                f"xAI OAuth account {account.label or account.id} "
                f"has no model mapped for tier {request.tier!s}",
            )
        try:
            auth = get_auth(_business_unit_id_from_request(request))
        except XaiOAuthError as exc:
            raise ProviderError(f"xAI auth: {exc}") from exc

        instructions, user_input = _split_system_from_messages(request.messages)
        payload: dict[str, Any] = {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": user_input,
        }
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        answer = ""
        reasoning = ""
        finish = "stop"
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
                    "POST", f"{XAI_API_BASE}/responses",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise ProviderError(
                            f"xAI Responses {resp.status_code}: "
                            + body.decode("utf-8", errors="replace")[:400],
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
                                finish = (
                                    "length" if reason == "max_output_tokens"
                                    else "error"
                                )
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"xAI Responses transport: {type(exc).__name__}: {exc}",
            ) from exc

        if not answer.strip():
            raise ProviderError("xAI Responses returned no text content")

        return CompletionResponse(
            content=answer,
            tool_calls=(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=Decimal("0"),  # subscription-paid
            provider=self.name,
            model=model,
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
        if not model:
            raise ProviderError(
                f"xAI OAuth account {account.label or account.id} "
                f"has no model mapped for tier {request.tier!s}",
            )
        try:
            auth = get_auth(_business_unit_id_from_request(request))
        except XaiOAuthError as exc:
            raise ProviderError(f"xAI auth: {exc}") from exc

        instructions, user_input = _split_system_from_messages(request.messages)
        payload: dict[str, Any] = {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": user_input,
        }
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
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
                    "POST", f"{XAI_API_BASE}/responses",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise ProviderError(
                            f"xAI Responses {resp.status_code}: "
                            + body.decode("utf-8", errors="replace")[:400],
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
                f"xAI Responses transport: {type(exc).__name__}: {exc}",
            ) from exc

        yield StreamChunk(finish_reason=finish_reason or "stop")


def xai_oauth_provider() -> XaiResponsesProvider:
    """Factory matching the existing ``ollama_cloud_provider`` /
    ``opencode_go_provider`` pattern."""
    return XaiResponsesProvider()


__all__ = [
    "XaiResponsesProvider",
    "xai_oauth_provider",
]
