"""Built-in inference-provider profile registrations.

This module is the "type catalog" of providers Korpha ships with —
DeepSeek, Ollama Cloud, OpenCode Go/Zen, OpenRouter, local Ollama,
Codex CLI, Claude Code CLI. Each is a ``ProviderProfile`` registered
with ``provider_profile_registry`` at import time, so the picker /
setup CLI / dashboard / router can all enumerate "what's available."

Why a separate file: keeping the metadata next to the implementation
(in ``openai_compat.py``) would clutter the call layer with picker
strings, setup URLs, and tier-capability tables. Profile registration
is configuration data, not call code; it lives here.

Plugin authors copy this file's pattern when adding their own
providers from third-party packages.
"""
from __future__ import annotations

import shutil

from korpha.audit.model import InferenceTier
from korpha.inference.provider_profile import (
    ApiMode,
    CostHint,
    ProviderProfile,
    SetupField,
    TierCapability,
    provider_profile_registry,
)
from korpha.inference.providers.claude_code import ClaudeCodeProvider
from korpha.inference.providers.codex_cli import CodexCLIProvider
from korpha.inference.providers.openai_compat import (
    OpenAICompatibleProvider,
)
from korpha.inference.providers.xai_responses import xai_oauth_provider
from korpha.inference.registry import AuthType
from korpha.inference import xai_oauth as _xai_oauth


# ---------------------------------------------------------------------------
# DeepSeek (direct API) — frontier open-weights, cheap
# ---------------------------------------------------------------------------


def _deepseek_profile() -> ProviderProfile:
    return ProviderProfile(
        name="deepseek",
        label="DeepSeek (direct API)",
        provider_factory=lambda: OpenAICompatibleProvider(
            name="deepseek",
            base_url="https://api.deepseek.com/v1",
        ),
        auth_type=AuthType.API_KEY,
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="https://api.deepseek.com/v1",
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="deepseek-v4-pro",
                context_length=128_000,
                supports_streaming=True,
                supports_tool_use=True,
                supports_reasoning=True,
                cost=CostHint(input_per_1m_usd=0.27, output_per_1m_usd=1.10),
            ),
            InferenceTier.WORKHORSE: TierCapability(
                default_model="deepseek-v4-flash",
                context_length=128_000,
                supports_streaming=True,
                supports_tool_use=True,
                cost=CostHint(input_per_1m_usd=0.07, output_per_1m_usd=0.27),
            ),
        },
        setup_fields=[
            SetupField(
                env_var="DEEPSEEK_API_KEY",
                description="API key from your DeepSeek dashboard",
                setup_url="https://platform.deepseek.com/api_keys",
            ),
        ],
        setup_url="https://platform.deepseek.com",
        description=(
            "Direct DeepSeek API. Frontier open-weights reasoning "
            "(V4 Pro) at a fraction of closed-model cost. Recommended "
            "Pro-tier baseline for Korpha."
        ),
        source="builtin",
        emoji="🧠",
    )


# ---------------------------------------------------------------------------
# Ollama Cloud — open-weights via cloud GPUs, low-friction
# ---------------------------------------------------------------------------


def _ollama_cloud_profile() -> ProviderProfile:
    return ProviderProfile(
        name="ollama-cloud",
        label="Ollama Cloud",
        provider_factory=lambda: OpenAICompatibleProvider(
            name="ollama-cloud",
            base_url="https://ollama.com/v1",
        ),
        auth_type=AuthType.API_KEY,
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="https://ollama.com/v1",
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="deepseek-v4-pro:cloud",
                context_length=128_000,
                supports_streaming=True,
                supports_reasoning=True,
            ),
            InferenceTier.WORKHORSE: TierCapability(
                default_model="deepseek-v4-flash:cloud",
                context_length=128_000,
                supports_streaming=True,
            ),
        },
        setup_fields=[
            SetupField(
                env_var="OLLAMA_CLOUD_API_KEY",
                description="API key from your Ollama Cloud account",
                setup_url="https://ollama.com/settings/keys",
            ),
        ],
        setup_url="https://ollama.com",
        description=(
            "Ollama Cloud serves open-weights models via a hosted API. "
            "Same model lineup as DeepSeek direct, lower friction setup, "
            "slightly higher per-token cost."
        ),
        source="builtin",
        emoji="🦙",
    )


# ---------------------------------------------------------------------------
# OpenCode Go — $10/mo subscription tier, fixed open models
# ---------------------------------------------------------------------------


def _opencode_go_profile() -> ProviderProfile:
    return ProviderProfile(
        name="opencode-go",
        label="OpenCode Go ($10/mo)",
        provider_factory=lambda: OpenAICompatibleProvider(
            name="opencode-go",
            base_url="https://opencode.ai/zen/go/v1",
        ),
        auth_type=AuthType.API_KEY,
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="https://opencode.ai/zen/go/v1",
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="deepseek-v4-pro",
                context_length=128_000,
                supports_streaming=True,
                supports_reasoning=True,
            ),
            InferenceTier.WORKHORSE: TierCapability(
                default_model="deepseek-v4-flash",
                context_length=128_000,
                supports_streaming=True,
            ),
        },
        setup_fields=[
            SetupField(
                env_var="OPENCODE_API_KEY",
                description="API key from OpenCode Zen dashboard",
                setup_url="https://opencode.ai/keys",
            ),
        ],
        setup_url="https://opencode.ai",
        description=(
            "Subscription tier — flat $10/mo for open-weights "
            "(DeepSeek family). Best dollar-for-dollar for high-volume "
            "Workhorse usage; fills quotas faster than Pro tier "
            "expects."
        ),
        source="builtin",
        emoji="🟢",
    )


# ---------------------------------------------------------------------------
# OpenCode Zen — premium subscription (Claude / GPT-5 / Opus)
# ---------------------------------------------------------------------------


def _opencode_zen_profile() -> ProviderProfile:
    return ProviderProfile(
        name="opencode-zen",
        label="OpenCode Zen (premium)",
        provider_factory=lambda: OpenAICompatibleProvider(
            name="opencode-zen",
            base_url="https://opencode.ai/zen/v1",
        ),
        auth_type=AuthType.API_KEY,
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="https://opencode.ai/zen/v1",
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="claude-opus-4.6",
                context_length=200_000,
                supports_streaming=True,
                supports_tool_use=True,
                supports_vision=True,
            ),
        },
        setup_fields=[
            SetupField(
                env_var="OPENCODE_ZEN_API_KEY",
                description="API key from OpenCode Zen premium dashboard",
                setup_url="https://opencode.ai/keys",
            ),
        ],
        setup_url="https://opencode.ai",
        description=(
            "Premium subscription — closed-model lineup (Claude Opus, "
            "GPT-5). Don't default here; Korpha recommends open "
            "weights. Available for users who specifically want it."
        ),
        source="builtin",
        emoji="💎",
    )


# ---------------------------------------------------------------------------
# OpenRouter — meta-aggregator
# ---------------------------------------------------------------------------


def _openrouter_profile() -> ProviderProfile:
    return ProviderProfile(
        name="openrouter",
        label="OpenRouter (paid)",
        provider_factory=lambda: OpenAICompatibleProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            extra_headers={
                "HTTP-Referer": "https://github.com/korpha/korpha",
            },
        ),
        auth_type=AuthType.API_KEY,
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="https://openrouter.ai/api/v1",
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="deepseek/deepseek-v4-pro",
                context_length=128_000,
                supports_streaming=True,
                supports_reasoning=True,
            ),
            InferenceTier.WORKHORSE: TierCapability(
                default_model="deepseek/deepseek-v4-flash",
                context_length=128_000,
            ),
        },
        setup_fields=[
            SetupField(
                env_var="OPENROUTER_API_KEY",
                description=(
                    "API key from OpenRouter with credit/balance — "
                    "for paid models. Use openrouter-free profile "
                    "for $0 free-tier keys."
                ),
                setup_url="https://openrouter.ai/keys",
            ),
        ],
        setup_url="https://openrouter.ai",
        description=(
            "Aggregator — one key, hundreds of models. PAID models "
            "only — needs balance on the OpenRouter account. For "
            "$0-balance free-tier keys, use 'OpenRouter (free)' "
            "profile instead."
        ),
        source="builtin",
        emoji="🔀",
    )


# ---------------------------------------------------------------------------
# OpenRouter (free tier) — $0 balance keys pinned to :free models
# ---------------------------------------------------------------------------
#
# OpenRouter offers a separate model lineup with a ``:free`` suffix
# (e.g. ``deepseek/deepseek-chat-v3:free``). These run at $0 but are
# rate-limited per-key (~50 requests/day on the free tier as of 2026).
#
# Why a separate profile from `openrouter`:
#   1. tier_models MUST end in `:free` — pointing a $0-balance key at
#      a paid model 401s silently. Hard-pinning prevents that footgun.
#   2. The cascade router can prefer free accounts for low-stakes work
#      and fall through to paid keys only when free quota is exhausted.
#   3. Free-tier rate limits are per-key, so plumbing N free keys as
#      N ProviderAccounts lets the PR-B 429-quota model rotate through
#      them automatically.


def _openrouter_free_profile() -> ProviderProfile:
    return ProviderProfile(
        name="openrouter-free",
        label="OpenRouter (free tier)",
        provider_factory=lambda: OpenAICompatibleProvider(
            name="openrouter-free",
            base_url="https://openrouter.ai/api/v1",
            extra_headers={
                "HTTP-Referer": "https://github.com/korpha/korpha",
            },
        ),
        auth_type=AuthType.API_KEY,
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="https://openrouter.ai/api/v1",
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="deepseek/deepseek-chat-v4:free",
                context_length=128_000,
                supports_streaming=True,
                supports_reasoning=True,
                cost=CostHint(input_per_1m_usd=0.0, output_per_1m_usd=0.0),
            ),
            InferenceTier.WORKHORSE: TierCapability(
                default_model="meta-llama/llama-3.3-70b-instruct:free",
                context_length=128_000,
                supports_streaming=True,
                cost=CostHint(input_per_1m_usd=0.0, output_per_1m_usd=0.0),
            ),
        },
        setup_fields=[
            SetupField(
                env_var="OPENROUTER_API_KEY_FREE",
                description=(
                    "Free-tier OpenRouter key (no balance loaded). "
                    "Add multiple via /app/credentials or `aigenteur "
                    "setup providers` — each becomes its own account "
                    "and the cascade rotates through them on 429."
                ),
                setup_url="https://openrouter.ai/keys",
            ),
        ],
        setup_url="https://openrouter.ai/keys",
        install_hint=(
            "Get free keys at https://openrouter.ai/keys (no balance "
            "needed). Add 10+ keys for production-grade volume — each "
            "gets ~50 req/day on the free tier."
        ),
        description=(
            "OpenRouter's $0 free-tier — pinned to :free model "
            "variants (DeepSeek V4 free, Llama 3.3 70B free, etc.). "
            "Rate-limited per key (~50/day); plumb multiple keys for "
            "throughput. Cost = $0 per token. Cascade rotates through "
            "them on 429 + falls through to paid providers when "
            "quota's exhausted."
        ),
        source="builtin",
        emoji="🆓",
    )


# ---------------------------------------------------------------------------
# Local Ollama — self-hosted, free
# ---------------------------------------------------------------------------


def _local_ollama_profile() -> ProviderProfile:
    return ProviderProfile(
        name="local-ollama",
        label="Local Ollama",
        provider_factory=lambda: OpenAICompatibleProvider(
            name="local-ollama",
            base_url="http://localhost:11434/v1",
        ),
        auth_type=AuthType.API_KEY,  # ollama ignores the bearer but expects one
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="http://localhost:11434/v1",
        tier_capabilities={
            InferenceTier.WORKHORSE: TierCapability(
                default_model="llama3.3:70b",
                context_length=32_000,
            ),
        },
        setup_fields=[],  # no env needed for the default localhost case
        setup_url="https://ollama.com/download",
        install_hint="Install Ollama from https://ollama.com/download then `ollama pull llama3.3`",
        check_fn=lambda: bool(shutil.which("ollama")),
        description=(
            "Local-only, free, no quota. Runs on your machine — "
            "speed depends on your hardware. Recommended Workhorse "
            "tier when you have a GPU + want zero per-token cost."
        ),
        source="builtin",
        emoji="🏠",
    )


# ---------------------------------------------------------------------------
# Codex CLI — uses ChatGPT subscription (no API key)
# ---------------------------------------------------------------------------


def _codex_cli_profile() -> ProviderProfile:
    return ProviderProfile(
        name="codex-cli",
        label="Codex CLI (ChatGPT subscription)",
        provider_factory=lambda: CodexCLIProvider(),
        auth_type=AuthType.SUBSCRIPTION_CLI,
        api_mode=ApiMode.SUBSCRIPTION_CLI,
        base_url=None,
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="codex-default",
                context_length=200_000,
                supports_streaming=False,  # CLI returns final text
                supports_tool_use=True,
            ),
        },
        setup_fields=[],  # no env — codex login handles OAuth
        setup_url="https://openai.com/codex",
        install_hint="npm install -g @openai/codex && codex login",
        check_fn=lambda: bool(shutil.which("codex")),
        description=(
            "Wrapper around the Codex CLI. Uses your ChatGPT "
            "subscription — no per-token charge. Best for code-heavy "
            "tasks (CTO delegation). Quotas fill faster than people "
            "expect; pair with API-key providers for bulk work."
        ),
        source="builtin",
        emoji="🛠️",
    )


# ---------------------------------------------------------------------------
# Claude Code CLI — uses Claude Pro subscription
# ---------------------------------------------------------------------------


def _claude_code_profile() -> ProviderProfile:
    return ProviderProfile(
        name="claude-code-cli",
        label="Claude Code CLI (Claude Pro subscription)",
        provider_factory=lambda: ClaudeCodeProvider(),
        auth_type=AuthType.SUBSCRIPTION_CLI,
        api_mode=ApiMode.SUBSCRIPTION_CLI,
        base_url=None,
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="claude-code-default",
                context_length=200_000,
                supports_streaming=False,
                supports_tool_use=True,
                supports_vision=True,
            ),
        },
        setup_fields=[],
        setup_url="https://claude.com/claude-code",
        install_hint="npm install -g @anthropic-ai/claude-code && claude login",
        check_fn=lambda: bool(shutil.which("claude")),
        description=(
            "Wrapper around Claude Code CLI. Uses your Claude Pro/Max "
            "subscription. Excellent for code work, especially long-"
            "context refactors. Subject to subscription daily limits."
        ),
        source="builtin",
        emoji="🤖",
    )


# ---------------------------------------------------------------------------
# xAI Grok via OAuth — uses SuperGrok / X Premium+ subscription
# ---------------------------------------------------------------------------


def _xai_oauth_profile() -> ProviderProfile:
    return ProviderProfile(
        name="xai-oauth",
        label="xAI Grok (X Premium+ / SuperGrok subscription)",
        provider_factory=xai_oauth_provider,
        auth_type=AuthType.OAUTH,
        api_mode=ApiMode.CHAT_COMPLETIONS,
        base_url="https://api.x.ai/v1",
        tier_capabilities={
            InferenceTier.PRO: TierCapability(
                default_model="grok-4.20-0309-reasoning",
                context_length=256_000,
                supports_streaming=True,
                supports_tool_use=True,
                supports_reasoning=True,
                cost=CostHint(input_per_1m_usd=0.0, output_per_1m_usd=0.0),
            ),
            InferenceTier.WORKHORSE: TierCapability(
                default_model="grok-4.20-0309-non-reasoning",
                context_length=256_000,
                supports_streaming=True,
                supports_tool_use=True,
                cost=CostHint(input_per_1m_usd=0.0, output_per_1m_usd=0.0),
            ),
        },
        setup_fields=[],  # no env — OAuth handles auth
        setup_url="https://x.com/i/premium_sign_up",
        install_hint=(
            "Sign in with your X Premium+ / SuperGrok account: "
            "`aigenteur auth add xai-oauth` (opens browser)."
        ),
        check_fn=_xai_oauth.is_configured,
        description=(
            "Wrapper around xAI's Responses API authenticated via your "
            "X Premium+ / SuperGrok subscription — no per-token charge. "
            "Burns subscription quota first; falls back to API key or "
            "next provider on 429. Pair with X Search skill for real-"
            "time tweet research."
        ),
        source="builtin",
        emoji="🐦",
    )


# ---------------------------------------------------------------------------
# Eager registration on import
# ---------------------------------------------------------------------------


def _register_all_builtins() -> None:
    for factory in (
        _deepseek_profile,
        _ollama_cloud_profile,
        _opencode_go_profile,
        _opencode_zen_profile,
        _openrouter_profile,
        _openrouter_free_profile,
        _local_ollama_profile,
        _codex_cli_profile,
        _claude_code_profile,
        _xai_oauth_profile,
    ):
        provider_profile_registry.register(factory())


_register_all_builtins()


__all__ = ["_register_all_builtins"]
