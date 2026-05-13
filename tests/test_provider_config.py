"""Tests for the YAML-driven provider config loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.config import (
    ProviderConfigError,
    load_from_yaml,
)
from korpha.inference.providers.openai_compat import PROVIDER_PRESETS


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "providers.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert load_from_yaml(tmp_path / "missing.yaml") is None


def test_loads_basic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_OPENCODE_KEY", "sk-test-123")
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: opencode-go
    label: my-opencode
    api_key_env: MY_OPENCODE_KEY
    tiers:
      workhorse: deepseek-v4-flash
      pro: deepseek-v4-pro
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert loaded.source == cfg
    assert len(loaded.providers) == 1
    assert loaded.providers[0].name == "opencode-go"
    assert len(loaded.accounts) == 1
    acc = loaded.accounts[0]
    assert acc.api_key == "sk-test-123"
    assert acc.label == "my-opencode"
    assert acc.tier_models[InferenceTier.WORKHORSE] == "deepseek-v4-flash"
    assert acc.tier_models[InferenceTier.PRO] == "deepseek-v4-pro"


def test_skips_entry_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNSET_KEY_ABC", raising=False)
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: opencode-go
    api_key_env: UNSET_KEY_ABC
    tiers: {workhorse: deepseek-v4-flash}
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert loaded.accounts == []


def test_multiple_keys_same_provider_for_parallelism(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DS_KEY_1", "key-one")
    monkeypatch.setenv("DS_KEY_2", "key-two")
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: deepseek
    label: ds-1
    api_key_env: DS_KEY_1
    tiers: {workhorse: deepseek-chat}
  - preset: deepseek
    label: ds-2
    api_key_env: DS_KEY_2
    tiers: {workhorse: deepseek-chat}
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    # Same provider only registered once, but both accounts present.
    assert len(loaded.providers) == 1
    assert len(loaded.accounts) == 2
    assert {a.api_key for a in loaded.accounts} == {"key-one", "key-two"}
    assert {a.label for a in loaded.accounts} == {"ds-1", "ds-2"}


def test_inline_api_key_for_tests(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: groq
    api_key: inline-secret
    tiers: {workhorse: llama-3.3-70b}
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert loaded.accounts[0].api_key == "inline-secret"


def test_unknown_preset_errors(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: bogus-provider
    api_key: x
    tiers: {workhorse: m}
""",
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        load_from_yaml(cfg)
    assert "bogus-provider" in str(excinfo.value)


def test_unknown_tier_errors(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: groq
    api_key: x
    tiers: {monstertier: m}
""",
    )
    with pytest.raises(ProviderConfigError):
        load_from_yaml(cfg)


def test_missing_tiers_errors(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: groq
    api_key: x
""",
    )
    with pytest.raises(ProviderConfigError):
        load_from_yaml(cfg)


def test_inline_and_env_both_set_errors(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: groq
    api_key: x
    api_key_env: GROQ_API_KEY
    tiers: {workhorse: m}
""",
    )
    with pytest.raises(ProviderConfigError):
        load_from_yaml(cfg)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    cfg = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ProviderConfigError):
        load_from_yaml(cfg)


def test_all_presets_buildable() -> None:
    """Every preset should construct without error so the YAML loader
    can dispatch by name."""
    for name, factory in PROVIDER_PRESETS.items():
        provider = factory()
        assert provider.name, f"{name} preset produced provider with empty name"
        assert provider.base_url.startswith("http"), f"{name} has bad base_url"


# ---------------------------------------------------------------------------
# preset: custom — arbitrary OpenAI-compat endpoints
# ---------------------------------------------------------------------------


def test_custom_preset_loads_with_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """preset: custom builds a Provider for any OpenAI-compat endpoint."""
    monkeypatch.setenv("MY_VLLM_KEY", "sk-test")
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: custom
    name: home-vllm
    base_url: http://10.0.0.5:8000/v1
    label: vllm-prod
    api_key_env: MY_VLLM_KEY
    tiers:
      workhorse: meta-llama/Llama-3-8B-Instruct
      pro: meta-llama/Llama-3-70B-Instruct
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert len(loaded.providers) == 1
    assert loaded.providers[0].name == "home-vllm"
    assert loaded.providers[0].base_url == "http://10.0.0.5:8000/v1"
    assert len(loaded.accounts) == 1
    assert loaded.accounts[0].label == "vllm-prod"
    assert loaded.accounts[0].api_key == "sk-test"
    assert loaded.accounts[0].tier_models[InferenceTier.WORKHORSE] == (
        "meta-llama/Llama-3-8B-Instruct"
    )


def test_custom_preset_accepts_extra_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K", "sk")
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: custom
    name: proxy
    base_url: https://proxy.internal/v1
    api_key_env: K
    tiers:
      pro: my-model
    extra_headers:
      X-Tenant: korpha
      X-Trace: enabled
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert loaded.providers[0].extra_headers == {
        "X-Tenant": "korpha",
        "X-Trace": "enabled",
    }


def test_custom_preset_strips_trailing_slash_on_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K", "sk")
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: custom
    name: x
    base_url: https://api.example.com/v1/
    api_key_env: K
    tiers:
      pro: m
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert loaded.providers[0].base_url == "https://api.example.com/v1"


def test_custom_preset_requires_base_url(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: custom
    name: x
    tiers:
      pro: m
""",
    )
    with pytest.raises(ProviderConfigError, match=r"requires.*base_url"):
        load_from_yaml(cfg)


def test_custom_preset_requires_name(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: custom
    base_url: https://x.com/v1
    tiers:
      pro: m
""",
    )
    with pytest.raises(ProviderConfigError, match=r"requires.*name"):
        load_from_yaml(cfg)


def test_custom_preset_rejects_non_http_base_url(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: custom
    name: x
    base_url: ftp://bad/v1
    tiers:
      pro: m
""",
    )
    with pytest.raises(ProviderConfigError, match="must start with"):
        load_from_yaml(cfg)


def test_custom_preset_rejects_collision_with_builtin_name(tmp_path: Path) -> None:
    """Picking name=openai for a custom preset would shadow the built-in;
    refuse rather than silently routing through a different endpoint."""
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: custom
    name: openai
    base_url: https://x.com/v1
    tiers:
      pro: m
""",
    )
    with pytest.raises(ProviderConfigError, match="collides"):
        load_from_yaml(cfg)


def test_codex_cli_preset_loads_without_api_key(tmp_path: Path) -> None:
    """Subscription preset doesn't carry an api_key — auth lives in
    `codex login`'s OAuth store. Loader should accept the entry and
    build a CodexCLIProvider."""
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: codex-cli
    label: chatgpt-sub
    tiers:
      workhorse: gpt-5-mini
      pro: gpt-5
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert len(loaded.providers) == 1
    assert loaded.providers[0].name == "codex-cli"
    assert len(loaded.accounts) == 1
    acc = loaded.accounts[0]
    assert acc.label == "chatgpt-sub"
    # Placeholder so the pool's "missing key" filter doesn't drop us.
    # Real auth is the codex login OAuth state on disk.
    assert acc.api_key == "subscription"
    assert acc.tier_models[InferenceTier.PRO] == "gpt-5"


def test_claude_code_cli_preset_loads_without_api_key(tmp_path: Path) -> None:
    """Same shape as codex-cli — Claude Pro / Max users."""
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: claude-code-cli
    label: claude-pro-sub
    tiers:
      workhorse: haiku
      pro: sonnet
""",
    )
    loaded = load_from_yaml(cfg)
    assert loaded is not None
    assert loaded.providers[0].name == "claude-code-cli"
    acc = loaded.accounts[0]
    assert acc.label == "claude-pro-sub"
    assert acc.api_key == "subscription"
    assert acc.tier_models[InferenceTier.PRO] == "sonnet"
    assert acc.tier_models[InferenceTier.WORKHORSE] == "haiku"


def test_codex_cli_preset_listed_in_unknown_preset_error(tmp_path: Path) -> None:
    """Error message should advertise codex-cli as a valid preset."""
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: not-real
    tiers:
      pro: m
""",
    )
    with pytest.raises(ProviderConfigError) as exc:
        load_from_yaml(cfg)
    assert "codex-cli" in str(exc.value)


def test_unknown_preset_error_lists_custom(tmp_path: Path) -> None:
    """Error message should advertise that 'custom' is an option."""
    cfg = _write(
        tmp_path,
        """
providers:
  - preset: not-a-real-preset
    tiers:
      pro: m
""",
    )
    with pytest.raises(ProviderConfigError) as exc:
        load_from_yaml(cfg)
    assert "custom" in str(exc.value)
