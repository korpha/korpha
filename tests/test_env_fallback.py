"""Tests for the env-var provider auto-detection matrix."""
from __future__ import annotations

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.env_fallback import (
    detect_configured_providers,
    list_configured_provider_names,
    list_supported_env_vars,
)


# ---- list_supported_env_vars ----


def test_matrix_includes_all_brief_providers() -> None:
    """BRIEF.md promises Nous Portal / OpenRouter / NVIDIA NIM /
    Xiaomi MiMo / z.ai / Kimi/Moonshot / MiniMax / HF / OpenAI.
    Plus first-class DeepSeek + the 'consultant slot' (which is
    Anthropic + OpenAI). Verify all are in the matrix."""
    names = {n for n, _ in list_supported_env_vars()}
    expected = {
        "ollama-cloud", "opencode-go", "opencode-zen",
        "openrouter", "deepseek", "openai", "anthropic",
        "groq", "cerebras", "together",
        "nous-portal", "nvidia-nim", "z-ai",
        "moonshot", "minimax", "huggingface", "xiaomi-mimo",
    }
    missing = expected - names
    assert not missing, f"missing presets: {missing}"


def test_env_vars_use_provider_specific_naming() -> None:
    """Check a couple representative names — XIAOMI_MIMO_API_KEY,
    NOUS_PORTAL_API_KEY, ZAI_API_KEY — to make sure we didn't
    typo. Mike pasting from documentation will hit these."""
    table = dict(list_supported_env_vars())
    assert table["xiaomi-mimo"] == "XIAOMI_MIMO_API_KEY"
    assert table["nous-portal"] == "NOUS_PORTAL_API_KEY"
    assert table["z-ai"] == "ZAI_API_KEY"
    assert table["nvidia-nim"] == "NVIDIA_API_KEY"
    assert table["moonshot"] == "MOONSHOT_API_KEY"
    assert table["openai"] == "OPENAI_API_KEY"
    assert table["anthropic"] == "ANTHROPIC_API_KEY"


# ---- detection ----


def test_detect_returns_empty_when_no_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear every env var the matrix reads
    for _, env_var in list_supported_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    assert detect_configured_providers() == []
    assert list_configured_provider_names() == []


def test_detect_picks_up_single_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _, env_var in list_supported_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    pairs = detect_configured_providers()
    assert len(pairs) == 1
    provider, account = pairs[0]
    assert provider.name == "openai"
    assert account.api_key == "sk-test"
    assert (
        InferenceTier.WORKHORSE in account.tier_models
        and InferenceTier.PRO in account.tier_models
    )


def test_detect_picks_up_multiple_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _, env_var in list_supported_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anth-1")

    names = [p.name for p, _ in detect_configured_providers()]
    assert set(names) == {"openai", "groq", "anthropic"}


def test_detect_returns_cost_ascending_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCode Go (subscription) should come before per-token
    providers when both are configured."""
    for _, env_var in list_supported_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-1")
    monkeypatch.setenv("OPENCODE_API_KEY", "oc-1")

    names = [p.name for p, _ in detect_configured_providers()]
    assert names[0] == "opencode-go"
    assert "openai" in names


def test_list_configured_provider_names_matches_detect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _, env_var in list_supported_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-1")
    monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf-1")

    detected = [p.name for p, _ in detect_configured_providers()]
    via_helper = list_configured_provider_names()
    assert detected == via_helper


def test_account_label_defaults_to_provider_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _, env_var in list_supported_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "k-1")

    _, account = detect_configured_providers()[0]
    assert account.label == "moonshot"


# ---- factory presets exposed at package level ----


def test_inference_pkg_exports_xiaomi_mimo() -> None:
    """Smoke test that the package re-exports work."""
    from korpha.inference import (
        anthropic_provider, huggingface_provider,
        moonshot_provider, nous_portal_provider,
        nvidia_nim_provider, openai_provider,
        xiaomi_mimo_provider, zai_provider,
    )
    # All callables, all return providers with a stable name
    p = xiaomi_mimo_provider()
    assert p.name == "xiaomi-mimo"
    assert "mimohub" in p.base_url


def test_xiaomi_mimo_in_preset_dict() -> None:
    from korpha.inference.providers.openai_compat import (
        PROVIDER_PRESETS,
    )
    assert "xiaomi-mimo" in PROVIDER_PRESETS


# ---- _build_pool_pieces uses the matrix ----


def test_build_pool_pieces_picks_up_arbitrary_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting just ANTHROPIC_API_KEY should populate the pool
    without any providers.yaml file."""
    for _, env_var in list_supported_env_vars():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anth-1")
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    from korpha.api.server import _build_pool_pieces

    providers, accounts = _build_pool_pieces()
    assert len(providers) == 1
    assert providers[0].name == "anthropic"
    assert accounts[0].api_key == "anth-1"
