"""Pluggable context engine for chat history compaction.

Port of Hermes's ``agent/context_engine.py`` and the core algorithm
from ``agent/context_compressor.py``, adapted for Korpha's
``Message`` dataclass (vs Hermes's dict-of-dicts) and Korpha's
``CostTracker`` (vs Hermes's standalone ``call_llm``).

The engine controls how the CEO's conversation history is shaped
before each LLM call. The default ``ContextCompressor`` protects
the head + tail of the conversation and summarizes the middle via
an auxiliary LLM. Threshold + protect-N values are configurable
through ``Settings`` so a 1M-context model can carry the full
business history through long real-business sessions, while small
local models still degrade gracefully.

Selection is config-driven via ``Settings.context_engine``.
Default ``"compressor"`` uses the built-in. Third-party engines
can register via plugins (mirrors Hermes's plugin system).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from korpha.audit.model import InferenceTier
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    from korpha.inference.types import Message


# Floor: never compress below this many tokens even when the
# threshold percentage would suggest a lower value. Stops premature
# compaction on small-context models, mirrors Hermes's constant.
MINIMUM_CONTEXT_LENGTH = 64_000

# Rough chars-per-token used by the cheap estimator. Same as Hermes.
_CHARS_PER_TOKEN = 4

# Flat per-image cost when estimating multimodal content (no image
# support in Korpha chat today — kept for parity).
_IMAGE_TOKEN_ESTIMATE = 1600


def estimate_message_tokens(msg: "Message") -> int:
    """Rough token count for a single ``Message``. No external
    tokenizer — pure char-based heuristic so this is safe to call
    on every turn without per-provider quirks. Off by ~20% in
    practice, which is fine for headroom calculations."""
    content = msg.content or ""
    chars = len(content)
    for tc in msg.tool_calls or ():
        try:
            chars += len(getattr(tc, "arguments_json", "")
                         or getattr(tc, "args", "") or "")
        except Exception:  # noqa: BLE001
            pass
    chars += len(msg.images or ()) * _IMAGE_TOKEN_ESTIMATE * _CHARS_PER_TOKEN
    # +10 to cover the role / name / tool_call_id metadata overhead.
    return (chars // _CHARS_PER_TOKEN) + 10


def estimate_messages_tokens(messages: list["Message"]) -> int:
    """Sum of ``estimate_message_tokens`` over a list. Same shape
    as Hermes's ``estimate_messages_tokens_rough``."""
    return sum(estimate_message_tokens(m) for m in messages)


class ContextEngine(ABC):
    """Base class every context engine must implement.

    Lifecycle: ``shape(messages, …)`` is called once per CEO turn
    before the LLM request fires. Implementations decide whether
    to trim, summarize, or pass through unchanged.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (``"compressor"``, ``"passthrough"``)."""

    # Runtime token state — populated by ``shape()`` for telemetry.
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0

    threshold_percent: float = 0.80
    protect_first_n: int = 3
    protect_last_n: int = 20

    @abstractmethod
    async def shape(
        self,
        messages: list["Message"],
        *,
        max_output_tokens: int,
        system_overhead_tokens: int = 0,
    ) -> list["Message"]:
        """Return the message list that should be sent to the LLM.

        ``max_output_tokens`` and ``system_overhead_tokens`` let
        the engine size the budget correctly — total prompt budget
        = ``context_length * threshold_percent - max_output - overhead``.
        """

    def get_status(self) -> dict[str, int | float]:
        return {
            "last_input_tokens": self.last_input_tokens,
            "last_output_tokens": self.last_output_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100.0, self.last_input_tokens / max(self.context_length, 1) * 100)
                if self.context_length else 0.0
            ),
            "compression_count": self.compression_count,
        }


class PassthroughContextEngine(ContextEngine):
    """Returns the message list unchanged. Useful for tests and as
    an explicit opt-out for installs that don't want any compaction.
    """

    @property
    def name(self) -> str:
        return "passthrough"

    async def shape(
        self,
        messages: list["Message"],
        *,
        max_output_tokens: int,
        system_overhead_tokens: int = 0,
    ) -> list["Message"]:
        self.last_input_tokens = estimate_messages_tokens(messages)
        return list(messages)


def resolve_context_length(
    pool: "InferencePool",
    tier: "InferenceTier",
) -> int:
    """Resolve the model context window for a given tier by inspecting
    every configured account that can serve it.

    Returns the **minimum** context_length across eligible providers
    so the compressor stays safe under cascade fallback (the smallest
    backend has to fit too). Falls back to ``MINIMUM_CONTEXT_LENGTH``
    when no profile reports a length for this tier.

    Does NOT invoke router.pick — pure inspection, no side effects.
    """
    from korpha.inference.provider_profile import (
        provider_profile_registry as registry,
    )
    lengths: list[int] = []
    for account in pool.accounts:
        profile = registry.get(account.provider_name)
        if profile is None:
            continue
        cap = profile.tier_capabilities.get(tier)
        if cap is None or not cap.context_length:
            continue
        lengths.append(int(cap.context_length))
    if not lengths:
        return MINIMUM_CONTEXT_LENGTH
    return max(MINIMUM_CONTEXT_LENGTH, min(lengths))


def build_context_engine(
    *,
    cost_tracker: "CostTracker",
    tier: "InferenceTier",
    session_key: str,
) -> ContextEngine:
    """Construct the configured engine. Reads ``Settings`` so every
    call site (CEO, VPs, future agents) gets the same defaults."""
    from korpha.audit.model import InferenceTier as _Tier
    from korpha.config import get_settings

    settings = get_settings()
    if settings.context_engine == "passthrough":
        return PassthroughContextEngine()

    context_length = resolve_context_length(
        cost_tracker.pool, tier,
    )
    # Lazy import to break the circular dependency (compressor →
    # cost_tracker → context_engine when both are imported eagerly).
    from korpha.cofounder.context_compressor import ContextCompressor

    return ContextCompressor(
        cost_tracker=cost_tracker,
        session_key=session_key,
        context_length=context_length,
        threshold_percent=settings.context_threshold_percent,
        protect_first_n=settings.context_protect_first_n,
        protect_last_n=settings.context_protect_last_n,
        summary_target_ratio=settings.context_summary_target_ratio,
        summary_tokens_ceiling=settings.context_summary_tokens_ceiling,
        summary_tier=_Tier.WORKHORSE,
    )


__all__ = [
    "MINIMUM_CONTEXT_LENGTH",
    "ContextEngine",
    "PassthroughContextEngine",
    "build_context_engine",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "resolve_context_length",
]
