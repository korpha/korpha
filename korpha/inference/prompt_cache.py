"""Anthropic 1-hour prompt-cache marker injection.

Anthropic's Messages API supports prompt caching with two TTLs:
ephemeral (5 minutes, default) and 1-hour. Marking the *stable*
parts of a prompt with ``cache_control: {type: "ephemeral", ttl:
"1h"}`` lets Anthropic skip re-tokenizing them on the next call
within 60 minutes — ~85-90% off input cost on returning sessions
where most of the prompt is the same agent persona + tool defs.

We apply two breakpoints by default:

  1. The system prompt (always the same per persona) — 1h TTL
  2. The last tool definition (tools rarely change mid-session) — 1h TTL

The volatile suffix (recent conversation turns) gets no cache mark
and is re-billed each call. Even on a 50k-token system prompt with a
2k-token recent-turn payload, the cache cuts cost from $0.45/call to
~$0.04/call after the first.

How to use: callers don't need to do anything. The OpenAI-compat
provider auto-detects Anthropic endpoints (api.anthropic.com or
explicit anthropic-version header) and inserts the markers before
sending.

References:
  https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
  Hermes PR #23828 (where this approach was first wired up)
"""
from __future__ import annotations

from typing import Any


# Models that support the 1h TTL marker. Everything else gets the
# default 5-minute ephemeral cache, which is still better than nothing.
# Newer Sonnet 4.x and Opus 4.x all support 1h; older Sonnet 3.5 + Haiku 3
# only support 5-minute.
_LONG_TTL_MODEL_PATTERNS: tuple[str, ...] = (
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    "claude-sonnet-5",  # forward-compat
    "claude-opus-5",
    "claude-haiku-5",
)


def is_anthropic_endpoint(base_url: str, headers: dict[str, str] | None = None) -> bool:
    """True when the request is headed for Anthropic. Drives whether
    we inject cache_control."""
    if "api.anthropic.com" in (base_url or "").lower():
        return True
    if headers:
        # Some self-hosted Anthropic-compat proxies set this header
        # explicitly so we recognize them.
        if (headers.get("anthropic-version") or "").strip():
            return True
    return False


def supports_long_ttl(model: str) -> bool:
    """True when the model accepts ``ttl: "1h"`` on cache_control.
    Falls back to default 5-min ephemeral when False."""
    if not model:
        return False
    m = model.lower()
    return any(p in m for p in _LONG_TTL_MODEL_PATTERNS)


def apply_cache_markers(
    payload: dict[str, Any], *, model: str,
) -> dict[str, Any]:
    """Mutate ``payload`` in-place to add Anthropic cache_control
    markers on the stable prefix (system prompt + last tool).
    Returns the same payload for chainable use.

    Idempotent — re-applying does no harm (the second application
    finds existing markers and skips).

    No-op when there's no system message and no tools (nothing to
    cache beyond the user turn, which we don't mark since it changes
    every call).
    """
    long_ttl = supports_long_ttl(model)
    cache_block: dict[str, Any] = {"type": "ephemeral"}
    if long_ttl:
        cache_block["ttl"] = "1h"

    messages = payload.get("messages") or []

    # 1. System prompt. In the OpenAI-compat shape this is messages[0]
    #    with role='system'. Anthropic's OAI-compat endpoint accepts
    #    cache_control on a system message via the content-blocks shape:
    #    {role: 'system', content: [{type:'text', text:'...',
    #     cache_control: {type:'ephemeral', ttl:'1h'}}]}
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content:
            # Convert string → content-blocks array with cache marker.
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": cache_block,
            }]
        elif isinstance(content, list) and content:
            # Already in content-blocks shape — mark the last block
            # (which is what Anthropic's docs recommend for system
            # prompts assembled from multiple parts).
            last = content[-1]
            if isinstance(last, dict) and "cache_control" not in last:
                last["cache_control"] = cache_block
        break  # only mark the first system message

    # 2. Last tool definition. Anthropic spec: cache_control on the
    #    tail of the tools array marks "everything up to and including
    #    this tool" as cacheable. Adds another cache breakpoint.
    tools = payload.get("tools") or []
    if tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict) and "cache_control" not in last_tool:
            last_tool["cache_control"] = cache_block

    return payload


__all__ = [
    "apply_cache_markers",
    "is_anthropic_endpoint",
    "supports_long_ttl",
]
