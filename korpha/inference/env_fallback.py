"""Env-var driven provider auto-detection.

Used by ``api/server.py::_build_pool_pieces`` (and any other
caller) to pick up every built-in provider whose API key is in
the environment. Mike sets ``OPENAI_API_KEY=...`` or
``ANTHROPIC_API_KEY=...`` and we route accordingly without him
editing providers.yaml.

Each preset has a stable env-var convention. The full matrix:

  ============== ============================ =====================================
  preset          env var                      provider factory
  ============== ============================ =====================================
  ollama-cloud    OLLAMA_CLOUD_API_KEY         ollama_cloud_provider
  opencode-go     OPENCODE_API_KEY             opencode_go_provider
  opencode-zen    OPENCODE_ZEN_API_KEY         opencode_zen_provider
  openrouter      OPENROUTER_API_KEY           openrouter_provider
  deepseek        DEEPSEEK_API_KEY             deepseek_provider
  openai          OPENAI_API_KEY               openai_provider
  anthropic       ANTHROPIC_API_KEY            anthropic_provider
  groq            GROQ_API_KEY                 groq_provider
  cerebras        CEREBRAS_API_KEY             cerebras_provider
  together        TOGETHER_API_KEY             together_provider
  nous-portal     NOUS_PORTAL_API_KEY          nous_portal_provider
  nvidia-nim      NVIDIA_API_KEY               nvidia_nim_provider
  z-ai            ZAI_API_KEY                  zai_provider
  moonshot        MOONSHOT_API_KEY             moonshot_provider
  minimax         MINIMAX_API_KEY              minimax_provider
  huggingface     HUGGINGFACE_API_KEY          huggingface_provider
  xiaomi-mimo     XIAOMI_MIMO_API_KEY          xiaomi_mimo_provider
  ============== ============================ =====================================

Order in the registry matters: the first detected provider in
``preferred_order`` becomes the default route. Subscription-tier
plays first (cheap), then OpenCode Go (subscription-style
pricing), then per-token providers in roughly cost-ascending
order. Mike with multiple keys gets the cheapest by default;
he can override by setting ``providers.yaml``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from korpha.audit.model import InferenceTier
from korpha.inference.providers.openai_compat import (
    OpenAICompatibleProvider,
    anthropic_provider,
    cerebras_provider,
    deepseek_provider,
    groq_provider,
    huggingface_provider,
    minimax_provider,
    moonshot_provider,
    nous_portal_provider,
    nvidia_nim_provider,
    ollama_cloud_provider,
    openai_provider,
    opencode_go_provider,
    opencode_zen_provider,
    openrouter_provider,
    together_provider,
    xiaomi_mimo_provider,
    zai_provider,
)
from korpha.inference.registry import AuthType, ProviderAccount


@dataclass(frozen=True)
class _PresetSpec:
    name: str
    env_var: str
    factory: Callable[[], OpenAICompatibleProvider]
    workhorse_model: str
    pro_model: str
    vision_model: Optional[str] = None
    """Model to use when InferenceTier.VISION is requested. Open-weights
    default for any provider that hosts a vision model; closed-source
    options stay as available choices but never the recommendation."""
    label: Optional[str] = None


# Cost-ascending order. Subscription-style + free-tier first.
_PRESETS: tuple[_PresetSpec, ...] = (
    _PresetSpec(
        name="opencode-go", env_var="OPENCODE_API_KEY",
        factory=opencode_go_provider,
        workhorse_model="deepseek-v4-flash",
        pro_model="deepseek-v4-pro",
    ),
    _PresetSpec(
        name="ollama-cloud", env_var="OLLAMA_CLOUD_API_KEY",
        factory=ollama_cloud_provider,
        workhorse_model="deepseek-v4-flash:cloud",
        pro_model="deepseek-v4-pro:cloud",
    ),
    _PresetSpec(
        name="opencode-zen", env_var="OPENCODE_ZEN_API_KEY",
        factory=opencode_zen_provider,
        workhorse_model="deepseek-v4-flash",
        pro_model="deepseek-v4-pro",
    ),
    _PresetSpec(
        name="deepseek", env_var="DEEPSEEK_API_KEY",
        factory=deepseek_provider,
        workhorse_model="deepseek-chat",
        pro_model="deepseek-reasoner",
    ),
    _PresetSpec(
        name="groq", env_var="GROQ_API_KEY",
        factory=groq_provider,
        workhorse_model="llama-3.3-70b-versatile",
        pro_model="deepseek-r1-distill-llama-70b",
    ),
    _PresetSpec(
        name="cerebras", env_var="CEREBRAS_API_KEY",
        factory=cerebras_provider,
        workhorse_model="llama-3.3-70b",
        pro_model="qwen-3-32b",
    ),
    _PresetSpec(
        name="together", env_var="TOGETHER_API_KEY",
        factory=together_provider,
        workhorse_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        pro_model="deepseek-ai/DeepSeek-V3",
    ),
    _PresetSpec(
        name="moonshot", env_var="MOONSHOT_API_KEY",
        factory=moonshot_provider,
        workhorse_model="kimi-k2-0905-preview",
        pro_model="kimi-k2-0905-preview",
    ),
    _PresetSpec(
        name="minimax", env_var="MINIMAX_API_KEY",
        factory=minimax_provider,
        workhorse_model="MiniMax-M1",
        pro_model="MiniMax-M1",
    ),
    _PresetSpec(
        name="z-ai", env_var="ZAI_API_KEY",
        factory=zai_provider,
        workhorse_model="glm-4-air",
        pro_model="glm-4-plus",
    ),
    _PresetSpec(
        name="nvidia-nim", env_var="NVIDIA_API_KEY",
        factory=nvidia_nim_provider,
        workhorse_model="meta/llama-3.3-70b-instruct",
        pro_model="deepseek-ai/deepseek-r1",
        # NVIDIA hosts the recommended open-weights vision default
        # (Nemotron 3 Nano Omni). Priority=11 in the cascade, so
        # Mike's vision calls hit NVIDIA first.
        vision_model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    ),
    _PresetSpec(
        name="nous-portal", env_var="NOUS_PORTAL_API_KEY",
        factory=nous_portal_provider,
        workhorse_model="Hermes-3-Llama-3.1-8B",
        pro_model="Hermes-3-Llama-3.1-70B",
    ),
    _PresetSpec(
        name="huggingface", env_var="HUGGINGFACE_API_KEY",
        factory=huggingface_provider,
        workhorse_model="meta-llama/Llama-3.3-70B-Instruct",
        pro_model="deepseek-ai/DeepSeek-V3",
    ),
    _PresetSpec(
        name="openrouter", env_var="OPENROUTER_API_KEY",
        factory=openrouter_provider,
        workhorse_model="deepseek/deepseek-chat",
        pro_model="deepseek/deepseek-r1",
        # OpenRouter routes to the free Nemotron 3 Nano Omni for
        # vision; fall through to this when NVIDIA isn't set or its
        # quota's gone. Mike's two-deep vision cascade.
        vision_model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    ),
    _PresetSpec(
        name="xiaomi-mimo", env_var="XIAOMI_MIMO_API_KEY",
        factory=xiaomi_mimo_provider,
        workhorse_model="MiMo-7B-Instruct",
        pro_model="MiMo-7B-RL",
    ),
    _PresetSpec(
        name="openai", env_var="OPENAI_API_KEY",
        factory=openai_provider,
        workhorse_model="gpt-4o-mini",
        pro_model="gpt-4o",
    ),
    _PresetSpec(
        name="anthropic", env_var="ANTHROPIC_API_KEY",
        factory=anthropic_provider,
        workhorse_model="claude-3-5-haiku-latest",
        pro_model="claude-sonnet-4-5",
    ),
)


def detect_configured_providers() -> list[tuple[
    OpenAICompatibleProvider, ProviderAccount,
]]:
    """Walk the preset list; for every env var that's set, build
    the (provider, account) pair. Returned in cost-ascending
    order so the pool routes to the cheapest by default.

    Each account gets a ``priority`` matching its preset index so
    the cascade respects cost order even without explicit config:
    OpenCode (1) → Ollama (2) → … → OpenRouter (14) → OpenAI (16) →
    Anthropic (17). Mike can override per-account in YAML."""
    out: list[tuple[OpenAICompatibleProvider, ProviderAccount]] = []
    for idx, spec in enumerate(_PRESETS):
        api_key = os.getenv(spec.env_var)
        if not api_key:
            continue
        tier_models: dict[InferenceTier, str] = {
            InferenceTier.WORKHORSE: spec.workhorse_model,
            InferenceTier.PRO: spec.pro_model,
        }
        # Only register the VISION tier when the preset explicitly
        # lists a vision_model. Otherwise vision routing would silently
        # fall onto a text-only model and 400 at request time.
        if spec.vision_model:
            tier_models[InferenceTier.VISION] = spec.vision_model
        account = ProviderAccount(
            provider_name=spec.name,
            auth_type=AuthType.API_KEY,
            tier_models=tier_models,
            api_key=api_key,
            label=spec.label or spec.name,
            priority=idx + 1,
        )
        out.append((spec.factory(), account))
    return out


def list_supported_env_vars() -> list[tuple[str, str]]:
    """Return ``(preset_name, env_var)`` for every supported
    preset. Used by ``korpha doctor`` + the README to
    document what Mike can set."""
    return [(p.name, p.env_var) for p in _PRESETS]


def list_configured_provider_names() -> list[str]:
    """Names of providers whose env var is currently set —
    useful for /healthz + doctor."""
    return [p.name for p in _PRESETS if os.getenv(p.env_var)]


__all__ = [
    "detect_configured_providers",
    "list_configured_provider_names",
    "list_supported_env_vars",
]
