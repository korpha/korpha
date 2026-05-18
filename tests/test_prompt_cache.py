"""Tests for the Anthropic prompt-cache marker injector."""
from __future__ import annotations

from korpha.inference.prompt_cache import (
    apply_cache_markers,
    is_anthropic_endpoint,
    supports_long_ttl,
)


# ---- endpoint detection -------------------------------------------


def test_anthropic_official_endpoint_detected():
    assert is_anthropic_endpoint("https://api.anthropic.com/v1") is True


def test_anthropic_header_marker_detected():
    assert is_anthropic_endpoint(
        "https://proxy.example.com/v1",
        headers={"anthropic-version": "2023-06-01"},
    ) is True


def test_non_anthropic_endpoint_not_detected():
    assert is_anthropic_endpoint("https://api.openai.com/v1") is False
    assert is_anthropic_endpoint("https://api.deepseek.com/v1") is False
    assert is_anthropic_endpoint("") is False


# ---- long-TTL model gate ------------------------------------------


def test_sonnet_4_supports_long_ttl():
    assert supports_long_ttl("claude-sonnet-4-7") is True
    assert supports_long_ttl("claude-opus-4-7") is True
    assert supports_long_ttl("claude-haiku-4-5") is True


def test_older_models_dont_support_long_ttl():
    assert supports_long_ttl("claude-sonnet-3-5") is False
    assert supports_long_ttl("claude-haiku-3") is False
    assert supports_long_ttl("gpt-4o") is False
    assert supports_long_ttl("") is False


# ---- cache marker injection ---------------------------------------


def test_system_string_converted_to_blocks_with_marker():
    payload = {
        "model": "claude-sonnet-4-7",
        "messages": [
            {"role": "system", "content": "you are an agent"},
            {"role": "user", "content": "hi"},
        ],
    }
    out = apply_cache_markers(payload, model="claude-sonnet-4-7")
    sys_msg = out["messages"][0]
    assert isinstance(sys_msg["content"], list)
    assert sys_msg["content"][0]["type"] == "text"
    assert sys_msg["content"][0]["text"] == "you are an agent"
    assert sys_msg["content"][0]["cache_control"] == {
        "type": "ephemeral", "ttl": "1h",
    }


def test_system_blocks_get_marker_on_last_block():
    payload = {
        "messages": [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "block A"},
                    {"type": "text", "text": "block B"},
                ],
            },
        ],
    }
    apply_cache_markers(payload, model="claude-sonnet-4-7")
    blocks = payload["messages"][0]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {
        "type": "ephemeral", "ttl": "1h",
    }


def test_short_ttl_for_older_models():
    payload = {
        "messages": [{"role": "system", "content": "x"}],
    }
    apply_cache_markers(payload, model="claude-sonnet-3-5")
    mark = payload["messages"][0]["content"][0]["cache_control"]
    assert mark == {"type": "ephemeral"}
    assert "ttl" not in mark


def test_last_tool_gets_marker():
    payload = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
            {"type": "function", "function": {"name": "c"}},
        ],
    }
    apply_cache_markers(payload, model="claude-sonnet-4-7")
    assert "cache_control" not in payload["tools"][0]
    assert "cache_control" not in payload["tools"][1]
    assert payload["tools"][2]["cache_control"] == {
        "type": "ephemeral", "ttl": "1h",
    }


def test_idempotent_reapplication():
    payload = {
        "messages": [{"role": "system", "content": "x"}],
        "tools": [{"type": "function", "function": {"name": "a"}}],
    }
    apply_cache_markers(payload, model="claude-sonnet-4-7")
    snap = str(payload)
    apply_cache_markers(payload, model="claude-sonnet-4-7")
    assert str(payload) == snap


def test_noop_when_no_system_and_no_tools():
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
    }
    apply_cache_markers(payload, model="claude-sonnet-4-7")
    # No system or tools to mark — payload should be unchanged.
    assert payload == {
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_only_first_system_message_marked():
    """If the caller (unusually) included multiple system messages,
    only the first gets a cache mark — the others fall through to
    the cache-miss path."""
    payload = {
        "messages": [
            {"role": "system", "content": "primary"},
            {"role": "system", "content": "secondary"},
            {"role": "user", "content": "hi"},
        ],
    }
    apply_cache_markers(payload, model="claude-sonnet-4-7")
    assert isinstance(payload["messages"][0]["content"], list)
    # Second system message stays a plain string.
    assert payload["messages"][1]["content"] == "secondary"


# ---- integration: payload from OpenAICompatibleProvider ----------


def test_anthropic_provider_payload_has_cache_markers():
    """The provider's _build_payload should auto-inject markers
    when base_url is Anthropic."""
    from korpha.audit.model import InferenceTier
    from korpha.inference.providers.openai_compat import (
        OpenAICompatibleProvider,
    )
    from korpha.inference.types import (
        CompletionRequest, Message, Role,
    )

    p = OpenAICompatibleProvider(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        extra_headers={"anthropic-version": "2023-06-01"},
    )
    req = CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content="be brief"),
            Message(role=Role.USER, content="hi"),
        ],
        tier=InferenceTier.PRO,
        session_key="test",
    )
    payload = p._build_payload(req, model="claude-sonnet-4-7")
    sys_block = payload["messages"][0]["content"][0]
    assert sys_block["cache_control"] == {
        "type": "ephemeral", "ttl": "1h",
    }


def test_non_anthropic_provider_skips_cache_markers():
    """OpenAI / DeepSeek / OpenRouter / etc — no cache_control
    injection (their APIs don't support it; would silently fail)."""
    from korpha.audit.model import InferenceTier
    from korpha.inference.providers.openai_compat import (
        OpenAICompatibleProvider,
    )
    from korpha.inference.types import (
        CompletionRequest, Message, Role,
    )

    p = OpenAICompatibleProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
    )
    req = CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content="be brief"),
            Message(role=Role.USER, content="hi"),
        ],
        tier=InferenceTier.PRO,
        session_key="test",
    )
    payload = p._build_payload(req, model="gpt-4o")
    # Plain string content, no markers anywhere.
    assert payload["messages"][0]["content"] == "be brief"
