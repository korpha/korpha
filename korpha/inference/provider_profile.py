"""Inference provider plugin contract — the public surface plugins use.

Two layers in the inference system:

  1. ``Provider`` (ABC, ``korpha.inference.provider``) — the low-level
     call interface. ``complete()`` and ``stream_complete()``. Hidden
     from plugin authors; they don't need to know the call shape unless
     they're writing a non-OpenAI-compatible adapter.
  2. ``ProviderProfile`` (this module) — the high-level metadata
     contract. Bundles a ``Provider`` factory with capability metadata
     (tier mapping, streaming, tool use, context length, API mode,
     setup schema). This is what plugins register; this is what the
     interactive setup CLI, the dashboard provider picker, and the
     router all read from.

Why a separate profile registry rather than expanding ``ProviderRegistry``:

  - ``ProviderRegistry`` holds *configured* instances + ``ProviderAccount``
    rows (one per credential). It's the runtime data store.
  - ``provider_profile_registry`` (this module) holds *available* providers
    you could configure. Empty accounts, just metadata + factory.
  - Plugins register profiles at import time. Users / setup CLI later
    create ``ProviderAccount`` rows from those profiles. Decoupling
    makes the discovery story plugin-friendly without coupling plugin
    load to credential availability.

Hermes Agent (``website/docs/developer-guide/adding-providers.md``)
documented a ``register_provider(ProviderProfile(...))`` API but never
shipped the symbol — Hermes contributors still edit 10 files in core
to add a provider. Korpha ships the contract Hermes promised.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from korpha.audit.model import InferenceTier
from korpha.inference.registry import AuthType

if TYPE_CHECKING:
    from korpha.inference.provider import Provider

logger = logging.getLogger(__name__)


class ApiMode(StrEnum):
    """How the provider's HTTP API is shaped.

    Selected by the request builder so we can mix OpenAI-compat
    providers with Anthropic / Bedrock / Codex without per-provider
    branches in the call sites. New shapes go here when added.
    """

    CHAT_COMPLETIONS = "chat_completions"
    """OpenAI-compatible /v1/chat/completions."""

    ANTHROPIC_MESSAGES = "anthropic_messages"
    """Anthropic's /v1/messages shape."""

    CODEX_RESPONSES = "codex_responses"
    """OpenAI Codex / Responses API shape."""

    SUBSCRIPTION_CLI = "subscription_cli"
    """Wrapper around a subprocess CLI (codex, claude-code) — no
    HTTP shape; the CLI handles its own auth + payload."""

    OTHER = "other"
    """Fallback for non-standard shapes the provider implements
    end-to-end inside its own ``Provider.complete``."""


@dataclass
class CostHint:
    """Rough per-million-token costs for a tier.

    ``None`` = "subscription / free / unknown — don't try to bill it
    against the spend cap." Callers that need exact numbers should
    look at ``ProviderAccount.pricing``, which is per-account and
    can override.
    """

    input_per_1m_usd: float | None = None
    output_per_1m_usd: float | None = None


@dataclass
class TierCapability:
    """What a profile offers for one inference tier.

    A profile may not serve every tier; if ``InferenceTier.PRO``
    has no entry, the profile cannot satisfy a Pro tier request.
    """

    default_model: str
    """Identifier the provider expects (e.g. ``"deepseek-v4-pro"``)."""

    context_length: int = 32_000
    """Max context window in tokens. Used by the memory layer to
    decide when to summarize. Default is conservative."""

    supports_streaming: bool = True
    supports_tool_use: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    """True for o1 / DeepSeek-V4-Pro / similar reasoning-trace models.
    Reasoning content is captured separately from message content
    when present — see ``CompletionResponse.reasoning``."""

    cost: CostHint = field(default_factory=CostHint)


@dataclass
class SetupField:
    """One env var / credential the user must supply.

    The interactive setup CLI iterates these fields, prompts with
    ``description`` + ``setup_url``, and writes the value to env or
    config. Mike-non-technical rule: this is the contract that
    keeps users away from YAML editing.
    """

    env_var: str
    """Env var name. Convention: PROVIDER_NAME_API_KEY."""

    description: str
    """What it is, in one short sentence Mike will read."""

    setup_url: str = ""
    """Where Mike clicks to get the value (e.g. signup URL,
    'create API key' page). Empty if N/A."""

    secret: bool = True
    """If True, the CLI masks input + redacts in logs."""

    optional: bool = False
    """If True, the field is nice-to-have. Setup proceeds without
    it. Used for things like ``ORG_ID`` on OpenAI."""


@dataclass
class ProviderProfile:
    """Plugin contract for an inference provider.

    A profile is a *type* of provider, not a configured instance.
    User credentials live on ``ProviderAccount`` rows which reference
    the profile by ``name``. One profile + many accounts is the
    normal shape (e.g. one ``deepseek`` profile, three accounts =
    three keys for parallelism).
    """

    name: str
    """Stable identifier. Lowercase, dashed (e.g. ``"deepseek"``,
    ``"opencode-go"``). Used as the FK from ``ProviderAccount``."""

    label: str
    """Human-readable, shown in CLI / dashboard ("DeepSeek", "Ollama
    Cloud")."""

    provider_factory: Callable[[], "Provider"]
    """Returns a fresh ``Provider`` instance ready to call. Plugins
    use this to inject extra config (custom base URL, retry policy,
    headers). Most plugins return ``OpenAICompatibleProvider(...)``."""

    auth_type: AuthType = AuthType.API_KEY
    """How authentication works. Drives the setup flow shape."""

    api_mode: ApiMode = ApiMode.CHAT_COMPLETIONS
    """HTTP / IPC payload shape for this provider."""

    base_url: str | None = None
    """OpenAI-compat base URL when applicable (``https://api.deepseek.com/v1``).
    None for subscription-CLI providers."""

    tier_capabilities: dict[InferenceTier, TierCapability] = field(default_factory=dict)
    """Per-tier defaults. The router reads ``default_model`` to pick
    a model when the user hasn't pinned one; callers read context
    length to decide compaction; tool-use / vision flags gate which
    skills are allowed to call this profile."""

    setup_fields: list[SetupField] = field(default_factory=list)
    """Env vars / credentials the interactive setup CLI prompts for.
    Order matters — list required first, then optional."""

    setup_url: str = ""
    """Top-level URL for the provider's signup / dashboard. Shown
    once at the top of the setup flow before per-field prompts."""

    install_hint: str = ""
    """Shown when ``check_fn`` returns False. Usually a one-liner
    ``pip install foo`` or ``brew install bar``."""

    check_fn: Callable[[], bool] = field(default=lambda: True)
    """Returns True when the provider's runtime dependencies are
    importable (e.g. ``codex`` binary on PATH for Codex CLI). Default
    True is correct for stdlib-only OpenAI-compat providers."""

    description: str = ""
    """One-paragraph description shown in the picker. What it's good
    at, who should use it. Don't market — describe."""

    source: str = "plugin"
    """``"builtin"`` or ``"plugin"``. Built-ins ship with Korpha;
    plugins come from ``~/.korpha/plugins/`` or pip entry points."""

    plugin_name: str = ""
    """Manifest name of the plugin that registered this profile.
    Empty for built-ins. Used to re-enable the plugin if the user
    configures one of its providers."""

    emoji: str = "🤖"
    """Display glyph for CLI / dashboard."""


class ProviderProfileRegistry:
    """Central registry of available inference-provider profiles.

    Thread-safe for reads (dict + GIL). Writes happen at startup.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, ProviderProfile] = {}

    def register(self, profile: ProviderProfile) -> None:
        """Add a profile. Last writer wins on collision so plugins
        can override built-ins when explicitly desired."""
        if profile.name in self._profiles:
            prev = self._profiles[profile.name]
            logger.info(
                "Provider '%s' re-registered (was %s, now %s)",
                profile.name, prev.source, profile.source,
            )
        self._profiles[profile.name] = profile
        logger.debug(
            "Registered provider profile: %s (%s)",
            profile.name, profile.source,
        )

    def unregister(self, name: str) -> bool:
        return self._profiles.pop(name, None) is not None

    def get(self, name: str) -> ProviderProfile | None:
        return self._profiles.get(name)

    def all_profiles(self) -> list[ProviderProfile]:
        return list(self._profiles.values())

    def builtin_profiles(self) -> list[ProviderProfile]:
        return [p for p in self._profiles.values() if p.source == "builtin"]

    def plugin_profiles(self) -> list[ProviderProfile]:
        return [p for p in self._profiles.values() if p.source == "plugin"]

    def is_registered(self, name: str) -> bool:
        return name in self._profiles

    def profiles_serving_tier(self, tier: InferenceTier) -> list[ProviderProfile]:
        """Return only profiles that have a TierCapability for
        ``tier``. Useful for the picker UI."""
        return [
            p for p in self._profiles.values()
            if tier in p.tier_capabilities
        ]

    def healthy_profiles(self) -> list[ProviderProfile]:
        """Return profiles whose ``check_fn`` returns True. Skips
        anything with missing native deps."""
        out: list[ProviderProfile] = []
        for p in self._profiles.values():
            try:
                if p.check_fn():
                    out.append(p)
            except Exception as exc:
                logger.warning(
                    "Provider '%s' check_fn raised: %s", p.name, exc,
                )
        return out


# Module-level singleton. Importing this module + calling .register()
# is the public registration surface for plugins and built-ins alike.
provider_profile_registry = ProviderProfileRegistry()


def register_inference_provider(profile: ProviderProfile) -> None:
    """Convenience wrapper. Plugins typically call this from their
    ``register(ctx)`` hook to add an inference backend."""
    provider_profile_registry.register(profile)


__all__ = [
    "ApiMode",
    "CostHint",
    "ProviderProfile",
    "ProviderProfileRegistry",
    "SetupField",
    "TierCapability",
    "provider_profile_registry",
    "register_inference_provider",
]
