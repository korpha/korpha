"""YAML-driven provider configuration.

Reads ``~/.korpha/providers.yaml`` (or a path given via
``KORPHA_PROVIDERS_FILE``) and produces ``(providers, accounts)`` lists
that ``InferencePool`` can consume directly.

Schema:

```yaml
# Optional global defaults — override the floors in
# ``korpha.inference.limits``. Every field is optional. Useful when
# you want to give a smaller model a tighter budget, or push reasoning
# models further. Floors stay safe even if this section is missing.
defaults:
  max_tokens_normal: 16000          # CEO, Director, Worker, skills
  max_tokens_coding: 128000         # coding loops (Codex CLI / Claude Code)
  agent_timeout_seconds: 300        # 5 min — generous for reasoning
  request_timeout_seconds: 60       # HTTP timeouts for non-LLM APIs

providers:
  - preset: opencode-go        # required: key in PROVIDER_PRESETS or "custom"
    label: opencode-primary    # optional: ProviderAccount.label
    api_key_env: OPENCODE_API_KEY
    # OR inline (discouraged but supported for tests)
    # api_key: sk-xxx
    tiers:
      workhorse: deepseek-v4-flash
      pro: deepseek-v4-pro
    concurrency_limit: 4       # optional, default 4
    spend_cap_usd: 25.00       # optional
    priority: 1                # optional, lower = tried first (default 100)
    retries_before_swap: 1     # optional, default 1 (1 retry on same acct before swap)

  - preset: ollama-cloud
    api_key_env: OLLAMA_CLOUD_API_KEY
    tiers:
      workhorse: deepseek-v4-flash:cloud
      pro: deepseek-v4-pro:cloud
    priority: 2                # Ollama fallback after OpenCode

  - preset: openrouter
    label: openrouter-paid
    api_key_env: OPENROUTER_API_KEY
    tiers:
      workhorse: deepseek/deepseek-chat
      pro: anthropic/claude-sonnet-4
    priority: 3                # paid bulk after subscription + local

  - preset: openrouter
    label: openrouter-free-key-1
    api_key_env: OPENROUTER_API_KEY_FREE_1
    tiers:
      workhorse: deepseek/deepseek-chat:free
    priority: 4                # free tier last
    # 429 on a free account = daily quota consumed, not "slow down".
    # Marks the account RATE_LIMITED until next reset_utc instead of
    # respecting retry_after.
    free_tier_quota:
      window_kind: daily
      reset_utc: "00:00"

  # Arbitrary OpenAI-compat endpoint not in the named preset list.
  # Used by the `korpha config` wizard for self-hosted / niche providers.
  - preset: custom
    name: my-vllm              # required for custom: stable identifier
    base_url: http://10.0.0.5:8000/v1  # required for custom
    label: home-vllm
    api_key_env: HOME_VLLM_KEY
    tiers:
      workhorse: meta-llama/Llama-3-8B-Instruct
      pro: meta-llama/Llama-3-70B-Instruct
    extra_headers:             # optional, e.g. for proxy auth
      X-Tenant: korpha
```

Order matters — earlier entries are tried first when no session affinity
exists. Multiple entries against the same preset (multiple keys) are fine
and feed the multi-account parallelism path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from korpha.audit.model import InferenceTier
from korpha.inference.provider import Provider
from korpha.inference.providers.claude_code import ClaudeCodeProvider
from korpha.inference.providers.codex_cli import CodexCLIProvider
from korpha.inference.providers.openai_compat import (
    PROVIDER_PRESETS,
    SUBSCRIPTION_PRESETS,
    OpenAICompatibleProvider,
)
from korpha.inference.registry import AuthType, ProviderAccount

DEFAULT_CONFIG_PATH = Path.home() / ".korpha" / "providers.yaml"


class ProviderConfigError(ValueError):
    """Raised when providers.yaml is malformed or references unknown presets."""


@dataclass
class LoadedConfig:
    providers: list[Provider]
    accounts: list[ProviderAccount]
    source: Path | None = None
    """Where the config was loaded from. None when env-var fallback was used."""


def config_path() -> Path:
    override = os.getenv("KORPHA_PROVIDERS_FILE")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_from_yaml(path: Path | None = None) -> LoadedConfig | None:
    """Parse providers.yaml. Returns None if the file doesn't exist."""
    p = path or config_path()
    if not p.exists():
        return None

    import yaml

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ProviderConfigError(f"{p}: top level must be a mapping, got {type(raw).__name__}")

    entries = raw.get("providers")
    if not isinstance(entries, list):
        raise ProviderConfigError(f"{p}: 'providers' must be a list")

    providers: list[Provider] = []
    accounts: list[ProviderAccount] = []
    seen_provider_names: set[str] = set()

    for idx, entry in enumerate(entries):
        provider, account = _parse_entry(entry, source=p, index=idx)
        if account is None:
            continue  # missing api key — skip silently
        if provider.name not in seen_provider_names:
            providers.append(provider)
            seen_provider_names.add(provider.name)
        accounts.append(account)

    return LoadedConfig(providers=providers, accounts=accounts, source=p)


def _parse_entry(
    entry: Any,
    *,
    source: Path,
    index: int,
) -> tuple[Provider, ProviderAccount | None]:
    if not isinstance(entry, dict):
        raise ProviderConfigError(
            f"{source}: providers[{index}] must be a mapping, got {type(entry).__name__}"
        )

    preset = entry.get("preset")
    if not isinstance(preset, str):
        raise ProviderConfigError(
            f"{source}: providers[{index}] missing required string 'preset'"
        )

    # Special "custom" preset: the entry itself supplies base_url + name
    # for any OpenAI-compat endpoint that isn't in the named-presets list.
    # Lets non-technical users wire up their own provider via the
    # `korpha config` wizard without touching code.
    provider: Provider
    if preset == "custom":
        provider = _build_custom_provider(entry, source=source, index=index)
    elif preset == "codex-cli":
        # Subscription auth via Codex CLI — no api_key required.
        # Mike runs `codex login` and the CLI's OAuth handles everything.
        provider = CodexCLIProvider()
    elif preset == "claude-code-cli":
        # Same shape for Claude Code: auth lives in keychain/OAuth set
        # up by `claude` on first run. ChatGPT subscription → codex-cli;
        # Claude Pro / Max → claude-code-cli; both pay $0 marginal.
        provider = ClaudeCodeProvider()
    elif preset in PROVIDER_PRESETS:
        provider = PROVIDER_PRESETS[preset]()
    else:
        known = ", ".join(sorted([*PROVIDER_PRESETS, *SUBSCRIPTION_PRESETS, "custom"]))
        raise ProviderConfigError(
            f"{source}: providers[{index}] unknown preset {preset!r}. Known: {known}"
        )

    tiers_raw = entry.get("tiers")
    if not isinstance(tiers_raw, dict) or not tiers_raw:
        raise ProviderConfigError(
            f"{source}: providers[{index}] needs a non-empty 'tiers' map "
            f"(e.g. tiers: {{workhorse: model-id}})"
        )

    tier_models: dict[InferenceTier, str] = {}
    for tier_name, model in tiers_raw.items():
        try:
            tier = InferenceTier(str(tier_name))
        except ValueError as exc:
            valid = ", ".join(t.value for t in InferenceTier)
            raise ProviderConfigError(
                f"{source}: providers[{index}] unknown tier {tier_name!r}. Valid: {valid}"
            ) from exc
        if not isinstance(model, str) or not model:
            raise ProviderConfigError(
                f"{source}: providers[{index}].tiers.{tier_name} must be a non-empty model id"
            )
        tier_models[tier] = model

    # Subscription presets (codex-cli, future claude-code-cli) don't use
    # api_key — auth is whatever `codex login` already set up. We still
    # need a non-None placeholder so the rest of the pipeline doesn't
    # treat the account as "missing key, skip it".
    api_key: str | None
    if preset in SUBSCRIPTION_PRESETS:
        api_key = "subscription"
    else:
        api_key = _resolve_api_key(entry, source=source, index=index)

    if api_key is None:
        return provider, None

    label = entry.get("label") or preset
    spend_cap = entry.get("spend_cap_usd")

    free_tier_quota = entry.get("free_tier_quota")
    if free_tier_quota is not None and not isinstance(free_tier_quota, dict):
        raise ProviderConfigError(
            f"{source}: providers[{index}].free_tier_quota must be a mapping "
            "with window_kind + reset_utc fields, e.g. "
            "{window_kind: daily, reset_utc: '00:00'}"
        )

    account = ProviderAccount(
        provider_name=provider.name,
        auth_type=AuthType.API_KEY,
        tier_models=tier_models,
        api_key=api_key,
        concurrency_limit=int(entry.get("concurrency_limit", 4)),
        spend_cap_usd=Decimal(str(spend_cap)) if spend_cap is not None else None,
        priority=int(entry.get("priority", 100)),
        retries_before_swap=int(entry.get("retries_before_swap", 1)),
        free_tier_quota=free_tier_quota,
        label=str(label),
    )
    return provider, account


def _build_custom_provider(
    entry: dict[str, Any], *, source: Path, index: int
) -> OpenAICompatibleProvider:
    """Build a Provider for a user-supplied OpenAI-compat endpoint.

    Required fields: ``base_url`` (the v1 root, e.g. ``https://api.x.com/v1``)
    and ``name`` (a stable identifier — used for session affinity hashing
    and in error messages).
    """
    base_url = entry.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ProviderConfigError(
            f"{source}: providers[{index}] preset 'custom' requires "
            "non-empty 'base_url' (e.g. https://api.example.com/v1)"
        )
    base_url = base_url.strip().rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ProviderConfigError(
            f"{source}: providers[{index}] base_url must start with "
            f"http:// or https://, got {base_url!r}"
        )

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProviderConfigError(
            f"{source}: providers[{index}] preset 'custom' requires "
            "non-empty 'name' (used for session affinity + error messages)"
        )
    name = name.strip()
    if name in PROVIDER_PRESETS:
        raise ProviderConfigError(
            f"{source}: providers[{index}] custom name {name!r} collides "
            "with a built-in preset — pick a different name"
        )

    extra_headers_raw = entry.get("extra_headers")
    extra_headers: dict[str, str] | None = None
    if extra_headers_raw is not None:
        if not isinstance(extra_headers_raw, dict):
            raise ProviderConfigError(
                f"{source}: providers[{index}].extra_headers must be a "
                "mapping of string→string"
            )
        extra_headers = {
            str(k): str(v) for k, v in extra_headers_raw.items()
        }

    return OpenAICompatibleProvider(
        name=name,
        base_url=base_url,
        extra_headers=extra_headers,
    )


def _resolve_api_key(entry: dict[str, Any], *, source: Path, index: int) -> str | None:
    inline = entry.get("api_key")
    env_name = entry.get("api_key_env")

    if inline and env_name:
        raise ProviderConfigError(
            f"{source}: providers[{index}] specifies both 'api_key' and 'api_key_env' — pick one"
        )
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    if isinstance(env_name, str) and env_name.strip():
        return os.getenv(env_name.strip())
    return None


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "LoadedConfig",
    "ProviderConfigError",
    "config_path",
    "load_from_yaml",
]
