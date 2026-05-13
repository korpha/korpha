"""OpenAICompatibleProvider — httpx-mocked unit tests."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from korpha.audit.model import InferenceTier
from korpha.inference import (
    CompletionRequest,
    Message,
    OpenAICompatibleProvider,
    ProviderAccount,
    Role,
    TierPricing,
)
from korpha.inference.provider import ProviderError, RateLimitError
from korpha.inference.registry import AuthType


def _account(api_key: str | None = "sk-test") -> ProviderAccount:
    return ProviderAccount(
        provider_name="ollama-cloud",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "deepseek-v4-flash:cloud",
            InferenceTier.PRO: "deepseek-v4-pro:cloud",
        },
        pricing={
            InferenceTier.PRO: TierPricing(
                input_per_1m_usd=Decimal("0.50"),
                output_per_1m_usd=Decimal("1.00"),
            ),
        },
        api_key=api_key,
        label="ollama-cloud-1",
    )


def _request(tier: InferenceTier = InferenceTier.PRO) -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role=Role.USER, content="Hello")],
        tier=tier,
        session_key="test-session",
    )


def _ollama_response_with_reasoning() -> dict[str, object]:
    """Shape returned by Ollama Cloud for thinking models like DeepSeek V4."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1000,
        "model": "deepseek-v4-pro:cloud",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello back!",
                    "reasoning": "The user said hello, I'll greet back.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 25,
            "total_tokens": 35,
        },
    }


@pytest.mark.asyncio
async def test_basic_completion_parses_content_and_reasoning() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=_ollama_response_with_reasoning())
    )
    provider = OpenAICompatibleProvider(name="ollama-cloud", base_url="https://ollama.com/v1")
    provider._client = httpx.AsyncClient(transport=transport)

    response = await provider.complete(_request(), _account())

    assert response.content == "Hello back!"
    assert response.reasoning == "The user said hello, I'll greet back."
    assert response.input_tokens == 10
    assert response.output_tokens == 25
    assert response.finish_reason == "stop"
    assert response.provider == "ollama-cloud"
    assert response.model == "deepseek-v4-pro:cloud"


@pytest.mark.asyncio
async def test_deepseek_native_reasoning_content_field() -> None:
    """DeepSeek's native API uses `reasoning_content` not `reasoning`."""
    payload = _ollama_response_with_reasoning()
    msg = payload["choices"][0]["message"]  # type: ignore[index]
    msg["reasoning_content"] = msg.pop("reasoning")  # type: ignore[index]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    provider = OpenAICompatibleProvider(name="deepseek", base_url="https://api.deepseek.com/v1")
    provider._client = httpx.AsyncClient(transport=transport)

    response = await provider.complete(_request(), _account())
    assert response.reasoning == "The user said hello, I'll greet back."


@pytest.mark.asyncio
async def test_no_reasoning_field_when_absent() -> None:
    payload = _ollama_response_with_reasoning()
    payload["choices"][0]["message"].pop("reasoning")  # type: ignore[index]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=transport)

    response = await provider.complete(_request(), _account())
    assert response.reasoning is None


@pytest.mark.asyncio
async def test_429_raises_rate_limit_error_with_retry_after() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(429, headers={"retry-after": "30"}, text="Too many")
    )
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=transport)

    with pytest.raises(RateLimitError) as exc_info:
        await provider.complete(_request(), _account())
    assert exc_info.value.retry_after_seconds == 30.0


@pytest.mark.asyncio
async def test_500_raises_provider_error() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="oops"))
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=transport)

    with pytest.raises(ProviderError):
        await provider.complete(_request(), _account())


@pytest.mark.asyncio
async def test_missing_api_key_raises() -> None:
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    with pytest.raises(ProviderError):
        await provider.complete(_request(), _account(api_key=None))


@pytest.mark.asyncio
async def test_tier_without_model_raises() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=transport)
    with pytest.raises(ProviderError):
        await provider.complete(_request(InferenceTier.CONSULTANT), _account())


@pytest.mark.asyncio
async def test_cached_tokens_from_prompt_tokens_details() -> None:
    """OpenAI cache-aware response: usage.prompt_tokens_details.cached_tokens."""
    payload = _ollama_response_with_reasoning()
    payload["usage"]["prompt_tokens_details"] = {"cached_tokens": 7}  # type: ignore[index]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=transport)

    response = await provider.complete(_request(), _account())
    assert response.cached_tokens == 7
    assert response.cache_hit_ratio == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_tool_calls_parsed() -> None:
    payload = _ollama_response_with_reasoning()
    payload["choices"][0]["message"]["tool_calls"] = [  # type: ignore[index]
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q": "ai cofounder"}'},
        }
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=transport)

    response = await provider.complete(_request(), _account())
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"q": "ai cofounder"}


@pytest.mark.asyncio
async def test_subscription_account_zero_cost_when_no_pricing() -> None:
    payload = _ollama_response_with_reasoning()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    provider = OpenAICompatibleProvider(name="ollama-cloud", base_url="https://ollama.com/v1")
    provider._client = httpx.AsyncClient(transport=transport)

    account = _account()
    account.pricing = {}  # subscription model — no per-token cost

    response = await provider.complete(_request(), account)
    assert response.cost_usd == Decimal("0")


@pytest.mark.asyncio
async def test_authorization_header_sent() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=_ollama_response_with_reasoning())

    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await provider.complete(_request(), _account(api_key="sk-secret"))

    assert captured["auth"] == "Bearer sk-secret"


@pytest.mark.asyncio
async def test_stream_complete_yields_chunks() -> None:
    """SSE-shaped response yields chunks with delta_content / delta_reasoning."""
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=transport)

    chunks = []
    async for c in provider.stream_complete(_request(), _account()):
        chunks.append(c)
    contents = [c.delta_content for c in chunks if c.delta_content]
    reasonings = [c.delta_reasoning for c in chunks if c.delta_reasoning]
    finishes = [c.finish_reason for c in chunks if c.finish_reason]
    assert "".join(contents) == "Hello"
    assert reasonings == ["thinking"]
    assert finishes == ["stop"]


@pytest.mark.asyncio
async def test_stream_complete_429_raises_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "15"}, text="rate limit")

    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from korpha.inference.provider import RateLimitError as _RLE

    with pytest.raises(_RLE):
        async for _ in provider.stream_complete(_request(), _account()):
            pass


@pytest.mark.asyncio
async def test_stream_complete_skips_unparseable_data_lines() -> None:
    sse_body = (
        b": comment line\n"
        b"data: not-json-here\n\n"
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    provider = OpenAICompatibleProvider(name="x", base_url="https://x")
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=sse_body))
    )
    chunks = [c async for c in provider.stream_complete(_request(), _account())]
    assert "".join(c.delta_content for c in chunks) == "ok"


@pytest.mark.asyncio
async def test_extra_headers_merged() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["referer"] = request.headers.get("http-referer", "")
        return httpx.Response(200, json=_ollama_response_with_reasoning())

    provider = OpenAICompatibleProvider(
        name="openrouter",
        base_url="https://x",
        extra_headers={"HTTP-Referer": "https://example.com"},
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await provider.complete(_request(), _account())
    assert captured["referer"] == "https://example.com"
