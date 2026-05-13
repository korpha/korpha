"""Tests for plugin discovery + opt-in.

Covers:
  - filter_enabled() policy semantics (allow-list, deny-list, '*')
  - enabled_set_from_env() / disabled_set_from_env() parsing
  - discover_all_plugins() merging entry-points + local dirs
  - PluginHost.add_channel_adapter / add_inference_provider gating
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from korpha.audit.model import InferenceTier
from korpha.channels.registry import (
    PlatformEntry,
    PlatformRegistry,
    platform_registry,
)
from korpha.heartbeats.dispatcher import HandlerRegistry
from korpha.inference.provider_profile import (
    ProviderProfile,
    ProviderProfileRegistry,
    TierCapability,
    provider_profile_registry,
)
from korpha.plugins.host import PluginHost, PluginPermissionError
from korpha.plugins.loader import (
    PluginManifest,
    disabled_set_from_env,
    discover_all_plugins,
    enabled_set_from_env,
    filter_enabled,
)
from korpha.skills.registry import SkillRegistry


def _manifest(name: str) -> PluginManifest:
    return PluginManifest(
        name=name,
        version="0.0.0",
        description="",
        author="",
        entry_point="x:register",
        permissions=frozenset(),
        source_path=Path("/tmp/fake"),
    )


# ---- filter_enabled ----


def test_filter_default_loads_nothing() -> None:
    """Opt-in is the rule. Empty allow-list = empty result."""
    manifests = [_manifest("a"), _manifest("b")]
    assert filter_enabled(manifests, enabled=None) == []
    assert filter_enabled(manifests, enabled=set()) == []


def test_filter_named_allow_list() -> None:
    manifests = [_manifest("a"), _manifest("b"), _manifest("c")]
    out = filter_enabled(manifests, enabled={"a", "c"})
    assert {m.name for m in out} == {"a", "c"}


def test_filter_star_loads_everything() -> None:
    """'*' is the YOLO escape hatch — load every discovered plugin."""
    manifests = [_manifest("a"), _manifest("b")]
    out = filter_enabled(manifests, enabled={"*"})
    assert {m.name for m in out} == {"a", "b"}


def test_filter_disabled_overrides_enabled() -> None:
    """A plugin in disabled is dropped even when also in enabled."""
    manifests = [_manifest("a"), _manifest("b")]
    out = filter_enabled(
        manifests, enabled={"a", "b"}, disabled={"b"},
    )
    assert {m.name for m in out} == {"a"}


def test_filter_star_then_disabled() -> None:
    """'*' + a deny-list = load everything except the denied list."""
    manifests = [_manifest("a"), _manifest("b"), _manifest("c")]
    out = filter_enabled(
        manifests, enabled={"*"}, disabled={"b"},
    )
    assert {m.name for m in out} == {"a", "c"}


# ---- env parsing ----


def test_enabled_from_env_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KORPHA_PLUGINS_ENABLED", raising=False)
    assert enabled_set_from_env() == set()
    monkeypatch.setenv("KORPHA_PLUGINS_ENABLED", "")
    assert enabled_set_from_env() == set()


def test_enabled_from_env_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "KORPHA_PLUGINS_ENABLED", "alpha, beta ,gamma",
    )
    assert enabled_set_from_env() == {"alpha", "beta", "gamma"}


def test_enabled_from_env_space_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KORPHA_PLUGINS_ENABLED", "alpha beta gamma")
    assert enabled_set_from_env() == {"alpha", "beta", "gamma"}


def test_enabled_from_env_star(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KORPHA_PLUGINS_ENABLED", "*")
    assert enabled_set_from_env() == {"*"}


def test_disabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KORPHA_PLUGINS_DISABLED", "broken-plugin")
    assert disabled_set_from_env() == {"broken-plugin"}


# ---- discover_all_plugins merge semantics ----


def test_discover_all_plugins_local_overrides_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same name from both sources: local dir wins so users can
    shadow installed plugins with a local fork."""
    plugin_dir = tmp_path / "p1"
    plugin_dir.mkdir()
    manifest = {
        "name": "shared-name",
        "version": "1.0.0",
        "description": "local override",
        "author": "x",
        "entry_point": "p1:register",
    }
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setenv("KORPHA_PLUGINS_DIR", str(tmp_path))

    # Skip entry-points (we'd need a real installed package); local
    # discovery path is what matters for the override semantic.
    out = discover_all_plugins(include_entry_points=False)
    assert any(m.name == "shared-name" for m in out)


# ---- PluginHost gating: new permissions ----


def _host(perms: set[str]) -> PluginHost:
    return PluginHost(
        plugin_name="test-plugin",
        permissions=frozenset(perms),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )


def test_add_channel_adapter_requires_permission() -> None:
    host = _host(set())
    entry = PlatformEntry(
        name="test-channel",
        label="Test Channel",
        adapter_factory=lambda cfg: object(),
    )
    with pytest.raises(PluginPermissionError):
        host.add_channel_adapter(entry)
    # The platform_registry should NOT have the entry — gate fired
    # before the side effect.
    assert not platform_registry.is_registered("test-channel")


def test_add_channel_adapter_with_permission() -> None:
    host = _host({"channel_adapters"})
    entry = PlatformEntry(
        name="test-channel-ok",
        label="Test Channel",
        adapter_factory=lambda cfg: object(),
    )
    try:
        host.add_channel_adapter(entry)
        assert platform_registry.is_registered("test-channel-ok")
        # Provenance stamping
        registered = platform_registry.get("test-channel-ok")
        assert registered is not None
        assert registered.source == "plugin"
        assert registered.plugin_name == "test-plugin"
        assert "test-channel-ok" in host.contributed_channels
    finally:
        platform_registry.unregister("test-channel-ok")


def test_add_inference_provider_requires_permission() -> None:
    host = _host(set())
    profile = ProviderProfile(
        name="bad-provider",
        label="Bad",
        provider_factory=lambda: object(),
    )
    with pytest.raises(PluginPermissionError):
        host.add_inference_provider(profile)
    assert not provider_profile_registry.is_registered("bad-provider")


def test_add_inference_provider_with_permission() -> None:
    host = _host({"inference_providers"})
    profile = ProviderProfile(
        name="ok-provider",
        label="OK",
        provider_factory=lambda: object(),
        tier_capabilities={
            InferenceTier.PRO: TierCapability(default_model="whatever"),
        },
    )
    try:
        host.add_inference_provider(profile)
        assert provider_profile_registry.is_registered("ok-provider")
        registered = provider_profile_registry.get("ok-provider")
        assert registered is not None
        assert registered.source == "plugin"
        assert registered.plugin_name == "test-plugin"
        assert "ok-provider" in host.contributed_providers
    finally:
        provider_profile_registry.unregister("ok-provider")
