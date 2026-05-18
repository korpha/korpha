"""Model-alias → (provider, real_model) routing table.

Lets external IDEs name a model like 'grok' or 'claude-opus' and
have the proxy route it to the right OAuth-backed Responses provider.

The catalog is plain code, not config, because:
  - The set of providers + their real model ids is stable across
    installs (changes only when a vendor releases a new model).
  - Adding/removing aliases is a code change that gets reviewed.
  - Aliases shipped here are sourced from each provider's
    ``provider_profile`` tier_capabilities so we don't drift.

Adding an alias: append to ``_ALIASES`` below. The proxy server
exposes them all via ``GET /v1/models`` so IDE pickers populate
automatically.
"""
from __future__ import annotations

from dataclasses import dataclass

from korpha.inference.xai_oauth import is_configured as xai_oauth_configured


@dataclass(frozen=True)
class ModelAlias:
    """One entry in the proxy's alias table.

    ``provider`` is one of:
      - ``"xai-oauth"``    — routes through xai_responses provider
      - ``"codex"``        — routes through codex_responses provider
      - ``"claude-code"``  — routes through the claude-code subprocess

    ``real_model`` is the upstream model id the chosen provider
    expects (e.g. ``grok-4.20-0309-reasoning``).
    """

    alias: str
    provider: str
    real_model: str
    description: str = ""

    def available(self) -> bool:
        """True iff the provider has working credentials on this install.
        Drives ``/v1/models`` filtering — we only advertise aliases the
        IDE can actually call."""
        if self.provider == "xai-oauth":
            return xai_oauth_configured()
        if self.provider == "codex":
            from korpha.inference.codex_oauth import is_configured
            return is_configured()
        if self.provider == "claude-code":
            import shutil
            return bool(shutil.which("claude"))
        return False


_ALIASES: tuple[ModelAlias, ...] = (
    # xAI / Grok via X Premium+ / SuperGrok subscription.
    ModelAlias(
        alias="grok",
        provider="xai-oauth",
        real_model="grok-4.20-0309-reasoning",
        description=(
            "Grok 4.20 Reasoning via X Premium+ subscription "
            "(best for code + planning)"
        ),
    ),
    ModelAlias(
        alias="grok-fast",
        provider="xai-oauth",
        real_model="grok-4.20-0309-non-reasoning",
        description=(
            "Grok 4.20 non-reasoning — faster, lower latency"
        ),
    ),
    ModelAlias(
        alias="grok-multi-agent",
        provider="xai-oauth",
        real_model="grok-4.20-multi-agent-0309",
        description="Grok 4.20 multi-agent variant",
    ),
    # OpenAI via ChatGPT Plus / Pro Codex subscription.
    ModelAlias(
        alias="gpt-5",
        provider="codex",
        real_model="gpt-5.4",
        description=(
            "GPT-5.4 via ChatGPT Plus/Pro Codex subscription"
        ),
    ),
    ModelAlias(
        alias="gpt",
        provider="codex",
        real_model="gpt-5.4",
        description="Alias for gpt-5",
    ),
    # Anthropic via Claude Pro/Max subscription.
    ModelAlias(
        alias="claude",
        provider="claude-code",
        real_model="claude-sonnet-4-7",
        description=(
            "Claude Sonnet 4.7 via Claude Pro/Max subscription"
        ),
    ),
    ModelAlias(
        alias="claude-sonnet",
        provider="claude-code",
        real_model="claude-sonnet-4-7",
        description="Alias for claude",
    ),
    ModelAlias(
        alias="claude-opus",
        provider="claude-code",
        real_model="claude-opus-4-7",
        description=(
            "Claude Opus 4.7 via Claude Pro/Max subscription "
            "(slower, higher quality)"
        ),
    ),
)


def all_aliases() -> tuple[ModelAlias, ...]:
    """The whole catalog including unavailable entries."""
    return _ALIASES


def available_aliases() -> list[ModelAlias]:
    """Only aliases whose upstream provider is configured on this
    install. This is what ``/v1/models`` returns."""
    return [a for a in _ALIASES if a.available()]


def resolve_alias(alias: str) -> ModelAlias | None:
    """Lookup by alias name. Lowercased + stripped before match.
    Returns None on unknown alias — the proxy surfaces this as a 404
    on /v1/chat/completions."""
    target = (alias or "").strip().lower()
    for a in _ALIASES:
        if a.alias == target:
            return a
    return None


__all__ = [
    "ModelAlias",
    "all_aliases",
    "available_aliases",
    "resolve_alias",
]
