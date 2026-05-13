"""Inference Pool — multi-key, multi-provider, cache-aware LLM routing.

Two architectural rules from BRIEF.md / ARCHITECTURE.md:

1. **Tiered routing**: Workhorse / Pro / Consultant. Workhorse handles routine,
   Pro handles all C-suite work, Consultant is escalation-only.
2. **Session affinity**: within an agent run, route to the same provider+key
   to maximize prompt-cache hits. Only swap on rate-limit / quota / failure.
"""
from __future__ import annotations

from korpha.inference.pool import InferencePool
from korpha.inference.provider_profile import (
    ApiMode,
    CostHint,
    ProviderProfile,
    ProviderProfileRegistry,
    SetupField,
    TierCapability,
    provider_profile_registry,
    register_inference_provider,
)
# Importing the builtins module eagerly registers DeepSeek, Ollama
# Cloud, OpenCode Go/Zen, OpenRouter, local Ollama, Codex CLI, and
# Claude Code with provider_profile_registry.
from korpha.inference.providers import builtins as _builtin_profiles  # noqa: F401
from korpha.inference.providers.mock import MockProvider
from korpha.inference.providers.openai_compat import (
    OpenAICompatibleProvider,
    anthropic_provider,
    cerebras_provider,
    deepseek_provider,
    groq_provider,
    huggingface_provider,
    local_ollama_provider,
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
from korpha.inference.registry import (
    ProviderAccount,
    ProviderRegistry,
    TierPricing,
)
from korpha.inference.router import InferenceRouter, RoutingError
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    Role,
    ToolCall,
)

__all__ = [
    "ApiMode",
    "CompletionRequest",
    "CompletionResponse",
    "CostHint",
    "InferencePool",
    "InferenceRouter",
    "Message",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ProviderAccount",
    "ProviderProfile",
    "ProviderProfileRegistry",
    "ProviderRegistry",
    "Role",
    "RoutingError",
    "SetupField",
    "TierCapability",
    "TierPricing",
    "ToolCall",
    "anthropic_provider",
    "cerebras_provider",
    "deepseek_provider",
    "groq_provider",
    "huggingface_provider",
    "local_ollama_provider",
    "minimax_provider",
    "moonshot_provider",
    "nous_portal_provider",
    "nvidia_nim_provider",
    "ollama_cloud_provider",
    "openai_provider",
    "opencode_go_provider",
    "opencode_zen_provider",
    "openrouter_provider",
    "provider_profile_registry",
    "register_inference_provider",
    "together_provider",
    "xiaomi_mimo_provider",
    "zai_provider",
]
