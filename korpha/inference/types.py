"""Shared types for the inference layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from korpha.audit.model import InferenceTier


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ImageRef:
    """A single image attached to a Message — used for the Vision tier.

    Either ``url`` (public HTTPS / data URL) or ``b64_png`` (raw bytes
    we'll wrap as a data URL at send time). Exactly one must be set.
    The OpenAI-compat spec (followed by every major open-weights vision
    model — Qwen-VL, Llama-3.2-Vision, Nemotron, Pixtral, GLM-4V) lets
    you mix text and image_url parts in the message content.
    """

    url: str | None = None
    b64_png: str | None = None
    """Base64-encoded PNG bytes (no ``data:`` prefix). Wrapped at send."""

    detail: str | None = None
    """OpenAI ``low`` | ``high`` | ``auto`` hint. Most open models ignore
    it; harmless to pass."""


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    images: tuple[ImageRef, ...] = ()
    """Optional image attachments. Empty for text-only turns. Providers
    that route via the Vision tier serialize these as multimodal
    ``content`` arrays; text-only providers raise if non-empty."""


@dataclass
class CompletionRequest:
    messages: list[Message]
    tier: InferenceTier
    session_key: str
    """Identifier scoping cache affinity. Same session_key → same account when possible."""

    max_tokens: int | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    stop: tuple[str, ...] = ()
    timeout_seconds: float = 60.0

    pinned_account_label: str | None = None
    """Optional account-label override. When set, the InferenceRouter
    routes this request to the matching account regardless of session
    affinity. Lets routines / heartbeats use a specific provider for
    a single call without changing global tier mappings.

    Falls back to normal routing if no account matches the label
    (logged warning, not an error — keeps the system resilient)."""


@dataclass(frozen=True)
class StreamChunk:
    """Incremental delta from a streaming completion.

    OpenAI-compatible providers send these as SSE frames. We surface a
    flattened shape so callers don't have to parse provider-specific
    JSON. ``finish_reason`` arrives only on the last chunk.
    """

    delta_content: str = ""
    delta_reasoning: str = ""
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None
    """The raw provider chunk dict, for debug. Don't depend on its shape."""


@dataclass
class CompletionResponse:
    content: str
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: Decimal
    provider: str
    model: str
    account_id: str
    """Which configured account served this — used for affinity tracking."""

    reasoning: str | None = None
    """Chain-of-thought text from thinking models (DeepSeek V4, Kimi K2 thinking,
    Claude with extended thinking, OpenAI o-series). Hidden from end users by
    default; surfaced for debugging or transparency. Reasoning tokens count
    toward output_tokens billing on most providers."""

    finish_reason: str | None = None
    cache_hit_ratio: float = 0.0
    """cached_tokens / max(input_tokens, 1). 1.0 = full prefix hit."""


__all__ = [
    "CompletionRequest",
    "CompletionResponse",
    "InferenceTier",
    "Message",
    "Role",
    "StreamChunk",
    "ToolCall",
]
