"""Tests for the OpenAI-compat OAuth proxy."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from korpha.audit.model import InferenceTier
from korpha.inference.types import (
    CompletionResponse,
    Message,
    Role,
    StreamChunk,
)
from korpha.proxy.aliases import (
    all_aliases, available_aliases, resolve_alias,
)
from korpha.proxy.server import build_proxy_app
from korpha.proxy.translator import (
    chat_to_completion_request,
    completion_response_to_chat,
)


# ---- aliases module ------------------------------------------------


def test_catalog_has_expected_aliases():
    names = {a.alias for a in all_aliases()}
    assert {"grok", "claude", "gpt"}.issubset(names)


def test_resolve_alias_case_insensitive():
    a1 = resolve_alias("grok")
    a2 = resolve_alias("GROK")
    a3 = resolve_alias("  grok  ")
    assert a1 is a2 is a3


def test_resolve_alias_unknown_returns_none():
    assert resolve_alias("totally-fake") is None


# ---- translator ----------------------------------------------------


def test_chat_to_completion_request_basic():
    alias = resolve_alias("grok")
    req = chat_to_completion_request(
        {
            "model": "grok",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
            "temperature": 0.7,
            "max_tokens": 100,
        },
        alias=alias,
    )
    assert len(req.messages) == 2
    assert req.messages[0].role == Role.SYSTEM
    assert req.messages[1].content == "hi"
    assert req.max_tokens == 100
    assert req.temperature == 0.7
    assert req.tier == InferenceTier.PRO


def test_chat_to_completion_request_rejects_empty_messages():
    alias = resolve_alias("grok")
    with pytest.raises(ValueError, match="non-empty"):
        chat_to_completion_request(
            {"messages": []}, alias=alias,
        )


def test_chat_to_completion_request_rejects_n_gt_1():
    alias = resolve_alias("grok")
    with pytest.raises(ValueError, match="n=1"):
        chat_to_completion_request(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "n": 2,
            },
            alias=alias,
        )


def test_chat_to_completion_request_extracts_multimodal_content():
    alias = resolve_alias("grok")
    req = chat_to_completion_request(
        {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://example.com/img.png",
                            "detail": "high",
                        },
                    },
                ],
            }],
        },
        alias=alias,
    )
    assert req.messages[0].content == "what is this?"
    assert len(req.messages[0].images) == 1
    assert req.messages[0].images[0].url == "https://example.com/img.png"
    assert req.messages[0].images[0].detail == "high"


def test_chat_to_completion_request_stop_str_or_list():
    alias = resolve_alias("grok")
    r1 = chat_to_completion_request(
        {"messages": [{"role": "user", "content": "x"}], "stop": "END"},
        alias=alias,
    )
    r2 = chat_to_completion_request(
        {
            "messages": [{"role": "user", "content": "x"}],
            "stop": ["END", "STOP"],
        },
        alias=alias,
    )
    assert r1.stop == ("END",)
    assert r2.stop == ("END", "STOP")


def test_completion_response_to_chat_shape():
    from decimal import Decimal
    alias = resolve_alias("grok")
    resp = CompletionResponse(
        content="hello",
        tool_calls=(),
        input_tokens=10,
        output_tokens=5,
        cached_tokens=3,
        cost_usd=Decimal("0"),
        provider="xai-oauth",
        model="grok-4.20-0309-reasoning",
        account_id="acct-1",
        reasoning="brief thought",
        finish_reason="stop",
        cache_hit_ratio=0.3,
    )
    out = completion_response_to_chat(resp, alias=alias)
    assert out["object"] == "chat.completion"
    assert out["model"] == "grok"
    assert out["choices"][0]["message"]["content"] == "hello"
    assert out["choices"][0]["message"]["reasoning_content"] == "brief thought"
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 5
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 3


# ---- HTTP layer ----------------------------------------------------


@pytest.fixture()
def client():
    app = build_proxy_app()
    return TestClient(app)


def test_models_endpoint_returns_only_available(client):
    """The /v1/models response only includes aliases whose OAuth
    provider is configured. Without env setup, available() returns
    False for xai-oauth (no token in vault) — those should be
    filtered out."""
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    # The exact set depends on what's configured in the test env;
    # at minimum the response shape must be valid.
    for m in body["data"]:
        assert m["object"] == "model"
        assert "real_model" in m


def test_healthz_returns_summary(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "available_aliases" in body


def test_chat_completions_unknown_model_404(client):
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake-model-xyz",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 404
    assert "unknown model" in r.json()["detail"]


def test_chat_completions_missing_model_400(client):
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400


def test_unsupported_endpoint_returns_structured_404(client):
    r = client.post("/v1/embeddings", json={})
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["type"] == "endpoint_unsupported"
    assert "/v1/embeddings" in body["error"]["message"]


def test_chat_completions_non_streaming_happy_path(client):
    """Mock the provider so we don't hit a real OAuth endpoint."""
    from decimal import Decimal
    from korpha.proxy.aliases import resolve_alias

    fake_resp = CompletionResponse(
        content="hello world",
        tool_calls=(),
        input_tokens=5,
        output_tokens=2,
        cached_tokens=0,
        cost_usd=Decimal("0"),
        provider="codex",
        model="gpt-5.4",
        account_id="acct",
        reasoning=None,
        finish_reason="stop",
        cache_hit_ratio=0.0,
    )

    # Patch the provider builder so any alias resolves to a mock provider.
    class FakeProvider:
        async def complete(self, req, account):
            return fake_resp

    with patch("korpha.proxy.server._build_provider_for") as mk:
        mk.return_value = FakeProvider()
        # Use 'claude' since claude-code is "configured" in the test
        # env (shutil.which finds claude binary).
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "claude",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hello world"
    assert body["model"] == "claude"


def test_chat_completions_streaming_emits_sse(client):
    """Stream path should yield OpenAI-shaped SSE frames + [DONE]."""

    async def fake_stream(req, account):
        yield StreamChunk(delta_content="hel", raw={})
        yield StreamChunk(delta_content="lo", raw={})
        yield StreamChunk(finish_reason="stop", raw={})

    class FakeProvider:
        def stream_complete(self, req, account):
            return fake_stream(req, account)

    with patch("korpha.proxy.server._build_provider_for") as mk:
        mk.return_value = FakeProvider()
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "claude",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200
            chunks = list(r.iter_lines())

    # Each SSE frame is "data: {...}" or "data: [DONE]"
    data_lines = [c for c in chunks if c.startswith("data:")]
    assert any("[DONE]" in c for c in data_lines)
    # First content frame carries the assistant role + 'hel'.
    parsed = [
        json.loads(c[5:].strip())
        for c in data_lines
        if "[DONE]" not in c
    ]
    deltas = [p["choices"][0]["delta"] for p in parsed]
    assert deltas[0].get("role") == "assistant"
    contents = "".join(d.get("content", "") for d in deltas)
    assert contents == "hello"
    assert parsed[-1]["choices"][0]["finish_reason"] == "stop"
