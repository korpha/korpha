"""OpenAI-compatible HTTP provider.

Works against any backend that implements the OpenAI Chat Completions API:
- Ollama Cloud (https://ollama.com/v1)
- DeepSeek (https://api.deepseek.com/v1)
- OpenRouter (https://openrouter.ai/api/v1)
- Together (https://api.together.xyz/v1)
- Local Ollama (http://localhost:11434/v1)
- Anthropic via OpenAI-compat endpoint
- Hosted vLLM, LM Studio, etc.

Reasoning content (DeepSeek V4 Pro/Flash, Kimi K2 thinking, etc.) is parsed from
either `message.reasoning` (Ollama Cloud / standard OpenAI-compat) or
`message.reasoning_content` (DeepSeek native API). Both are captured so the
cofounder can hide the chain-of-thought by default and surface it on demand.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from korpha.inference.provider import Provider, ProviderError, RateLimitError
from korpha.inference.providers.mock import _compute_cost
from korpha.inference.registry import ProviderAccount
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    StreamChunk,
    ToolCall,
)


@dataclass
class OpenAICompatibleProvider(Provider):
    """Generic Provider for any OpenAI-compatible Chat Completions endpoint."""

    name: str
    base_url: str
    """Base URL up to (but not including) `/chat/completions`. e.g. `https://ollama.com/v1`."""

    timeout_seconds: float = 60.0
    extra_headers: dict[str, str] | None = None
    _client: httpx.AsyncClient | None = None

    async def stream_complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> AsyncIterator[StreamChunk]:
        """Stream chunks via OpenAI-style SSE (``stream: true``)."""
        if account.api_key is None:
            raise ProviderError(
                f"Account {account.label or account.id} has no api_key for streaming call"
            )
        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"Account {account.label or account.id} has no model for tier {request.tier!s}"
            )

        payload = self._build_payload(request, model)
        payload["stream"] = True
        client = self._get_client()
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        try:
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers=self._build_headers(account),
                timeout=request.timeout_seconds or self.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    body_bytes = await response.aread()
                    body_text = body_bytes[:2000].decode(
                        "utf-8", errors="replace",
                    )
                    from korpha.inference.errors import (
                        FailoverReason, classify,
                    )
                    classified = classify(
                        status_code=response.status_code, body=body_text,
                    )
                    if classified.should_rotate_credential or classified.reason in (
                        FailoverReason.RATE_LIMIT,
                        FailoverReason.OVERLOADED,
                        FailoverReason.AUTH,
                        FailoverReason.BILLING,
                    ):
                        retry_after = float(
                            response.headers.get("retry-after", "30"),
                        )
                        raise RateLimitError(
                            account_id=str(account.id),
                            retry_after_seconds=retry_after,
                            classified=classified,
                        )
                    raise ProviderError(
                        f"{self.name} returned {response.status_code}: "
                        f"{body_text[:500]}",
                        classified=classified,
                    )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_raw = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    parsed = _parse_stream_chunk(chunk_raw)
                    if parsed is not None:
                        yield parsed
        except httpx.TimeoutException as exc:
            from korpha.inference.errors import classify
            raise ProviderError(
                f"Timeout streaming from {self.name}: {exc}",
                classified=classify(exc=exc),
            ) from exc
        except httpx.RequestError as exc:
            from korpha.inference.errors import classify
            raise ProviderError(
                f"Network error streaming from {self.name}: {exc}",
                classified=classify(exc=exc),
            ) from exc

    async def complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> CompletionResponse:
        if account.api_key is None:
            raise ProviderError(
                f"Account {account.label or account.id} has no api_key for OpenAI-compat call"
            )

        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"Account {account.label or account.id} has no model for tier {request.tier!s}"
            )

        payload = self._build_payload(request, model)
        client = self._get_client()

        try:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=self._build_headers(account),
                timeout=request.timeout_seconds or self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            from korpha.inference.errors import classify
            classified = classify(exc=exc)
            raise ProviderError(
                f"Timeout calling {self.name}: {exc}",
                classified=classified,
            ) from exc
        except httpx.RequestError as exc:
            from korpha.inference.errors import classify
            classified = classify(exc=exc)
            raise ProviderError(
                f"Network error calling {self.name}: {exc}",
                classified=classified,
            ) from exc

        # Run the structured classifier on every >=400 response so the
        # router gets recovery hints (should_rotate, should_compress,
        # should_fallback) without re-parsing string bodies. We keep
        # the existing exception types for backward compat — RateLimit
        # for anything that says "rotate this account", ProviderError
        # for everything else — but stash the ClassifiedError on the
        # exception so callers can opt into the richer view.
        if response.status_code >= 400:
            from korpha.inference.errors import (
                FailoverReason, classify,
            )
            classified = classify(
                status_code=response.status_code,
                body=response.text,
            )
            if classified.should_rotate_credential or classified.reason in (
                FailoverReason.RATE_LIMIT,
                FailoverReason.OVERLOADED,
                FailoverReason.AUTH,
                FailoverReason.BILLING,
            ):
                retry_after = float(
                    response.headers.get("retry-after", "30")
                )
                raise RateLimitError(
                    account_id=str(account.id),
                    retry_after_seconds=retry_after,
                    classified=classified,
                )
            raise ProviderError(
                f"{self.name} returned {response.status_code}: {response.text[:500]}",
                classified=classified,
            )

        return self._parse_response(response.json(), request=request, account=account, model=model)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _build_headers(self, account: ProviderAccount) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {account.api_key}",
            "Content-Type": "application/json",
        }
        if self.extra_headers:
            headers.update(self.extra_headers)
        return headers

    def _build_payload(
        self, request: CompletionRequest, model: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_message_to_openai(m) for m in request.messages],
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = list(request.tools)
        if request.stop:
            payload["stop"] = list(request.stop)
        # Anthropic 1-hour prompt cache: when sending to Anthropic
        # (api.anthropic.com or a proxy that opts in via
        # anthropic-version header), mark the stable prefix (system
        # prompt + last tool def) with cache_control so the next call
        # within 60 minutes pays only output tokens. ~85-90% off
        # first-turn input cost on returning sessions.
        from korpha.inference.prompt_cache import (
            apply_cache_markers, is_anthropic_endpoint,
        )
        if is_anthropic_endpoint(self.base_url, self.extra_headers):
            apply_cache_markers(payload, model=model)
        return payload

    def _parse_response(
        self,
        data: dict[str, Any],
        *,
        request: CompletionRequest,
        account: ProviderAccount,
        model: str,
    ) -> CompletionResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name} returned no choices: {data!r}")

        choice0 = choices[0]
        message = choice0.get("message") or {}
        content = message.get("content") or ""

        # Reasoning lives under different keys depending on backend.
        # Ollama Cloud / vLLM / common OpenAI-compat:  message.reasoning
        # DeepSeek native API:                          message.reasoning_content
        # OpenAI o-series:                              choice.reasoning (less common)
        reasoning = (
            message.get("reasoning")
            or message.get("reasoning_content")
            or choice0.get("reasoning")
            or None
        )

        tool_calls = _parse_tool_calls(message.get("tool_calls") or [])

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)

        # OpenAI-compat token-cache field: prompt_tokens_details.cached_tokens
        cached_tokens = 0
        details = usage.get("prompt_tokens_details") or {}
        if "cached_tokens" in details:
            cached_tokens = int(details["cached_tokens"] or 0)
        elif "cache_read_input_tokens" in usage:  # Anthropic-style
            cached_tokens = int(usage["cache_read_input_tokens"] or 0)

        cost = self._estimate_cost(
            account=account,
            tier=request.tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )

        return CompletionResponse(
            content=content,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=cost,
            provider=self.name,
            model=model,
            account_id=str(account.id),
            reasoning=reasoning if reasoning else None,
            finish_reason=choice0.get("finish_reason"),
            cache_hit_ratio=cached_tokens / input_tokens if input_tokens else 0.0,
        )

    @staticmethod
    def _estimate_cost(
        account: ProviderAccount,
        tier: Any,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> Decimal:
        pricing = account.pricing.get(tier)
        if pricing is None:
            # Subscription-backed accounts (Ollama Cloud, ChatGPT plan) have no per-token cost.
            return Decimal("0")
        return _compute_cost(pricing, input_tokens, output_tokens, cached_tokens)


def _parse_stream_chunk(chunk: dict[str, Any]) -> StreamChunk | None:
    """Pull delta_content / delta_reasoning out of an OpenAI-shaped SSE chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    finish = choices[0].get("finish_reason")
    return StreamChunk(
        delta_content=str(delta.get("content") or ""),
        delta_reasoning=str(
            delta.get("reasoning") or delta.get("reasoning_content") or ""
        ),
        finish_reason=finish,
        raw=chunk,
    )


def _message_to_openai(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value}
    # Multimodal: when images attached, content becomes an array of
    # parts (text first, then image_url entries). This is the OpenAI-
    # compat shape every major open-weights vision model speaks
    # (Qwen-VL, Llama-3.2-Vision, Pixtral, Nemotron, GLM-4V).
    if message.images:
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"type": "text", "text": message.content})
        for img in message.images:
            url = img.url
            if url is None and img.b64_png:
                url = f"data:image/png;base64,{img.b64_png}"
            if url is None:
                continue
            entry: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
            if img.detail is not None:
                entry["image_url"]["detail"] = img.detail
            parts.append(entry)
        payload["content"] = parts
    else:
        payload["content"] = message.content
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        payload["name"] = message.name
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in message.tool_calls
        ]
    return payload


def _parse_tool_calls(raw: list[dict[str, Any]]) -> tuple[ToolCall, ...]:
    parsed: list[ToolCall] = []
    for tc in raw:
        function = tc.get("function") or {}
        args = function.get("arguments") or {}
        if isinstance(args, str):
            import json

            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        parsed.append(
            ToolCall(
                id=str(tc.get("id", "")),
                name=str(function.get("name", "")),
                arguments=args,
            )
        )
    return tuple(parsed)


# Convenience preset: Ollama Cloud
def ollama_cloud_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="ollama-cloud",
        base_url="https://ollama.com/v1",
    )


# Convenience preset: OpenCode Zen (premium tier — Claude / GPT-5 / Opus)
def opencode_zen_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="opencode-zen",
        base_url="https://opencode.ai/zen/v1",
    )


# Convenience preset: OpenCode Go ($10/mo tier — open models, faster, lower limits)
def opencode_go_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
    )


# Convenience preset: DeepSeek direct API
def deepseek_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
    )


# Convenience preset: OpenRouter (paid models — requires balance on the account)
def openrouter_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        extra_headers={"HTTP-Referer": "https://github.com/korpha/korpha"},
    )


# Convenience preset: OpenRouter free-tier — separate provider so we
# can hard-enforce :free model suffix at config-load time. A $0-balance
# key pointed at a paid model 401s silently; pinning the suffix prevents
# that footgun.
def openrouter_free_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="openrouter-free",
        base_url="https://openrouter.ai/api/v1",
        extra_headers={"HTTP-Referer": "https://github.com/korpha/korpha"},
    )


# Convenience preset: local Ollama (no auth needed but still uses bearer)
def local_ollama_provider(host: str = "http://localhost:11434") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="local-ollama",
        base_url=f"{host}/v1",
    )


# Convenience preset: LM Studio local server (OpenAI-compat at
# localhost:1234 by default — LM Studio's "Local Server" feature
# exposes the OpenAI chat completions API for any model you've
# downloaded inside the app).
def lm_studio_provider(host: str = "http://localhost:1234") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="lm-studio",
        base_url=f"{host}/v1",
    )


# Convenience preset: Together AI
def together_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="together",
        base_url="https://api.together.xyz/v1",
    )


# Convenience preset: Groq
def groq_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
    )


# Convenience preset: Cerebras
def cerebras_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
    )


# Convenience preset: Anthropic via OpenAI-compat shim
# (use the official Anthropic OAI compat: api.anthropic.com/v1)
def anthropic_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        extra_headers={"anthropic-version": "2023-06-01"},
    )


# Convenience preset: OpenAI direct
def openai_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
    )


# Convenience preset: Nous Portal
def nous_portal_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="nous-portal",
        base_url="https://inference-api.nousresearch.com/v1",
    )


# Convenience preset: NVIDIA NIM (build.nvidia.com)
def nvidia_nim_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="nvidia-nim",
        base_url="https://integrate.api.nvidia.com/v1",
    )


# Convenience preset: Z.ai (GLM models)
def zai_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="z-ai",
        base_url="https://api.z.ai/api/paas/v4",
    )


# Convenience preset: Moonshot / Kimi
def moonshot_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="moonshot",
        base_url="https://api.moonshot.ai/v1",
    )


# Convenience preset: MiniMax
def minimax_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="minimax",
        base_url="https://api.minimax.io/v1",
    )


# Convenience preset: Hugging Face Inference Endpoints (OpenAI-compat router)
def huggingface_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="huggingface",
        base_url="https://router.huggingface.co/v1",
    )


# Convenience preset: Xiaomi MiMo
# (https://www.mimohub.cn — open-weights small LM family from Xiaomi)
def xiaomi_mimo_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="xiaomi-mimo",
        base_url="https://api.mimohub.cn/v1",
    )


# Map preset NAME → factory. Used by the YAML config loader.
PROVIDER_PRESETS: dict[str, Callable[[], OpenAICompatibleProvider]] = {
    "deepseek": deepseek_provider,
    "local-ollama": local_ollama_provider,
    "lm-studio": lm_studio_provider,
    "ollama-cloud": ollama_cloud_provider,
    "opencode-go": opencode_go_provider,
    "opencode-zen": opencode_zen_provider,
    "openrouter": openrouter_provider,
    "openrouter-free": openrouter_free_provider,
    "together": together_provider,
    "groq": groq_provider,
    "cerebras": cerebras_provider,
    "anthropic": anthropic_provider,
    "openai": openai_provider,
    "nous-portal": nous_portal_provider,
    "nvidia-nim": nvidia_nim_provider,
    "z-ai": zai_provider,
    "moonshot": moonshot_provider,
    "minimax": minimax_provider,
    "huggingface": huggingface_provider,
    "xiaomi-mimo": xiaomi_mimo_provider,
}

# Subscription-auth presets that don't speak HTTP — we shell out to a
# CLI that already handles OAuth refresh. Kept in a separate dict so the
# config loader can route them through different code paths (no api_key,
# no base_url, etc.). Both keys are valid in providers.yaml.
SUBSCRIPTION_PRESETS: tuple[str, ...] = ("codex-cli", "claude-code-cli")


__all__ = [
    "PROVIDER_PRESETS",
    "OpenAICompatibleProvider",
    "anthropic_provider",
    "cerebras_provider",
    "deepseek_provider",
    "groq_provider",
    "huggingface_provider",
    "local_ollama_provider",
    "minimax_provider",
    "moonshot_provider",
    "nous_portal_provider",
    "nvidia_nim_provider",
    "ollama_cloud_provider",
    "openai_provider",
    "opencode_go_provider",
    "opencode_zen_provider",
    "openrouter_provider",
    "together_provider",
    "xiaomi_mimo_provider",
    "zai_provider",
]
