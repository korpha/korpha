"""Tests for the interactive setup CLI.

Two layers:
  1. Pure helpers (read/write yaml, save+restore round-trip) —
     fast, no CLI invocation needed.
  2. ``typer.testing.CliRunner`` smoke tests for the entry points.

We avoid testing the full interactive prompt loop with stdin scripting
since typer.prompt + hide_input is fiddly under runner.invoke; the
integration test there would be brittle. The ``_walk_setup_fields``
helper is exercised indirectly via the save round-trip tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from korpha.audit.model import InferenceTier
from korpha.channels.registry import (
    PlatformEntry,
    PlatformRegistry,
    platform_registry,
)
from korpha.cli import app
from korpha.cli_setup import (
    _existing_channel_envs,
    _existing_provider_envs,
    _save_channel_setup,
    _save_provider_setup,
    disable_plugin,
    enable_plugin,
)
from korpha.inference.provider_profile import (
    ProviderProfile,
    SetupField,
    TierCapability,
    provider_profile_registry,
)
from korpha.inference.registry import AuthType


# ---- save / restore round-trip ----


def test_save_provider_setup_writes_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    profile = provider_profile_registry.get("deepseek")
    assert profile is not None

    path = _save_provider_setup(profile, {"DEEPSEEK_API_KEY": "sk-test"})
    assert path == tmp_path / "providers.yaml"
    body = yaml.safe_load(path.read_text())
    providers = body["providers"]
    assert len(providers) == 1
    entry = providers[0]
    assert entry["preset"] == "deepseek"
    assert entry["setup_envs"]["DEEPSEEK_API_KEY"] == "sk-test"
    # Tier mapping populates from the profile's tier_capabilities
    assert "pro" in entry["tiers"]


def test_save_provider_setup_replaces_same_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running setup for the same provider replaces the entry
    rather than appending — Mike updating his key shouldn't end up
    with two deepseek rows."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    profile = provider_profile_registry.get("deepseek")
    assert profile is not None
    _save_provider_setup(profile, {"DEEPSEEK_API_KEY": "old"})
    _save_provider_setup(profile, {"DEEPSEEK_API_KEY": "new"})

    body = yaml.safe_load((tmp_path / "providers.yaml").read_text())
    assert len(body["providers"]) == 1
    assert body["providers"][0]["setup_envs"]["DEEPSEEK_API_KEY"] == "new"


def test_save_provider_setup_appends_different_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    deepseek = provider_profile_registry.get("deepseek")
    openrouter = provider_profile_registry.get("openrouter")
    assert deepseek is not None and openrouter is not None
    _save_provider_setup(deepseek, {"DEEPSEEK_API_KEY": "ds-key"})
    _save_provider_setup(openrouter, {"OPENROUTER_API_KEY": "or-key"})

    body = yaml.safe_load((tmp_path / "providers.yaml").read_text())
    presets = {p["preset"] for p in body["providers"]}
    assert presets == {"deepseek", "openrouter"}


def test_existing_provider_envs_returns_empty_for_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    assert _existing_provider_envs("never-configured") == {}


def test_existing_provider_envs_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    profile = provider_profile_registry.get("deepseek")
    assert profile is not None
    _save_provider_setup(profile, {"DEEPSEEK_API_KEY": "sk-x"})

    envs = _existing_provider_envs("deepseek")
    assert envs == {"DEEPSEEK_API_KEY": "sk-x"}


def test_save_channel_setup_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    entry = platform_registry.get("telegram")
    assert entry is not None
    _save_channel_setup(entry, {"KORPHA_TELEGRAM_TOKEN": "bot-1234"})

    envs = _existing_channel_envs("telegram")
    assert envs == {"KORPHA_TELEGRAM_TOKEN": "bot-1234"}


def test_save_channel_setup_replaces_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    entry = platform_registry.get("telegram")
    assert entry is not None
    _save_channel_setup(entry, {"KORPHA_TELEGRAM_TOKEN": "old"})
    _save_channel_setup(entry, {"KORPHA_TELEGRAM_TOKEN": "new"})
    body = yaml.safe_load((tmp_path / "channels.yaml").read_text())
    assert len(body["channels"]) == 1
    assert body["channels"][0]["setup_envs"]["KORPHA_TELEGRAM_TOKEN"] == "new"


# ---- file permissions ----


def test_provider_yaml_has_tight_perms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """providers.yaml may carry inline secrets — chmod 600 protects
    against shared-system leakage."""
    import os

    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    profile = provider_profile_registry.get("deepseek")
    assert profile is not None
    _save_provider_setup(profile, {"DEEPSEEK_API_KEY": "sk-secret"})

    mode = os.stat(tmp_path / "providers.yaml").st_mode & 0o777
    # On systems that support it the writer chmods to 0o600. Any
    # mode tighter than world-readable is acceptable.
    assert (mode & 0o077) == 0, f"providers.yaml leaks perms: {oct(mode)}"


# ---- plugin enable/disable ----


def test_plugin_enable_then_disable_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    enable_plugin("test-plugin")
    body = yaml.safe_load((tmp_path / "plugins.yaml").read_text())
    assert "test-plugin" in body.get("enabled", [])

    # Disabling should remove from enabled + add to disabled
    disable_plugin("test-plugin")
    body = yaml.safe_load((tmp_path / "plugins.yaml").read_text())
    assert "test-plugin" in body.get("disabled", [])
    assert "test-plugin" not in body.get("enabled", [])


def test_plugin_enable_clears_disabled_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a plugin is currently disabled and the user enables it,
    we drop it from the deny-list. Enable wins."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    disable_plugin("flip-flop")
    enable_plugin("flip-flop")
    body = yaml.safe_load((tmp_path / "plugins.yaml").read_text())
    assert "flip-flop" in body.get("enabled", [])
    assert "flip-flop" not in body.get("disabled", [])


# ---- CLI smoke tests via CliRunner ----


_runner = CliRunner()


def test_setup_providers_lists_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    result = _runner.invoke(app, ["setup", "providers"])
    assert result.exit_code == 0
    # Should list at least the canonical built-ins by name
    assert "deepseek" in result.output
    assert "ollama-cloud" in result.output
    assert "Inference providers" in result.output


def test_setup_providers_unknown_name_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    result = _runner.invoke(app, ["setup", "providers", "no-such-thing"])
    assert result.exit_code == 2
    assert "No provider profile" in result.output


def test_setup_channels_lists_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    result = _runner.invoke(app, ["setup", "channels"])
    assert result.exit_code == 0
    assert "telegram" in result.output.lower()


def test_setup_plugins_lists_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    result = _runner.invoke(app, ["setup", "plugins"])
    assert result.exit_code == 0
    assert "Plugin status" in result.output


def test_setup_plugins_enable_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    result = _runner.invoke(app, ["setup", "plugins", "enable", "demo-plugin"])
    assert result.exit_code == 0
    body = yaml.safe_load((tmp_path / "plugins.yaml").read_text())
    assert "demo-plugin" in body["enabled"]


def test_setup_plugins_unknown_action_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    result = _runner.invoke(app, ["setup", "plugins", "smash", "x"])
    assert result.exit_code == 2
    assert "Unknown action" in result.output


def test_setup_plugins_enable_without_name_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    result = _runner.invoke(app, ["setup", "plugins", "enable"])
    assert result.exit_code == 2
    assert "needs a plugin name" in result.output


# ---- subscription-CLI provider path (no env vars needed) ----


def test_setup_subscription_provider_writes_marker_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscription providers (codex-cli, claude-code-cli) have no
    env vars — setup should still record a marker entry so the
    runtime knows they're configured + the catalog shows ✓."""
    import shutil

    # Codex profile's check_fn looks for the binary on PATH; make it
    # available so the subscription path actually fires (otherwise we
    # exit 2 with "deps missing").
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/fake")
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    result = _runner.invoke(app, ["setup", "providers", "codex-cli"])
    assert result.exit_code == 0
    body = yaml.safe_load((tmp_path / "providers.yaml").read_text())
    presets = {p["preset"] for p in body["providers"]}
    assert "codex-cli" in presets
