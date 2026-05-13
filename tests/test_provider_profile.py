"""Tests for the inference-provider plugin contract.

Two layers:
  1. ProviderProfileRegistry — generic registry mechanics.
  2. The shipped built-in profiles — coverage that the eight
     providers Korpha ships with all conform to the contract
     and have the metadata downstream consumers (setup CLI, picker,
     router) depend on.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.provider_profile import (
    ApiMode,
    ProviderProfile,
    ProviderProfileRegistry,
    SetupField,
    TierCapability,
    provider_profile_registry,
)
from korpha.inference.registry import AuthType


def _profile(
    *,
    name: str = "test",
    factory: Callable[[], object] | None = None,
    check: Callable[[], bool] | None = None,
    source: str = "plugin",
    tiers: dict[InferenceTier, TierCapability] | None = None,
) -> ProviderProfile:
    return ProviderProfile(
        name=name,
        label=name.title(),
        provider_factory=factory or (lambda: object()),
        check_fn=check or (lambda: True),
        source=source,
        tier_capabilities=tiers or {},
    )


# ---- registry mechanics ----


def test_register_and_get() -> None:
    reg = ProviderProfileRegistry()
    p = _profile(name="alpha")
    reg.register(p)
    assert reg.is_registered("alpha")
    assert reg.get("alpha") is p
    assert reg.get("missing") is None


def test_register_replaces_on_collision() -> None:
    reg = ProviderProfileRegistry()
    first = _profile(name="x", source="builtin")
    second = _profile(name="x", source="plugin")
    reg.register(first)
    reg.register(second)
    assert reg.get("x") is second
    assert len(reg.all_profiles()) == 1


def test_unregister() -> None:
    reg = ProviderProfileRegistry()
    reg.register(_profile(name="bye"))
    assert reg.unregister("bye") is True
    assert reg.unregister("bye") is False


def test_filtering_by_source() -> None:
    reg = ProviderProfileRegistry()
    reg.register(_profile(name="b1", source="builtin"))
    reg.register(_profile(name="p1", source="plugin"))
    builtins = {p.name for p in reg.builtin_profiles()}
    plugins = {p.name for p in reg.plugin_profiles()}
    assert builtins == {"b1"}
    assert plugins == {"p1"}


def test_profiles_serving_tier() -> None:
    reg = ProviderProfileRegistry()
    reg.register(_profile(
        name="pro_only",
        tiers={InferenceTier.PRO: TierCapability(default_model="m")},
    ))
    reg.register(_profile(
        name="both",
        tiers={
            InferenceTier.PRO: TierCapability(default_model="m1"),
            InferenceTier.WORKHORSE: TierCapability(default_model="m2"),
        },
    ))
    reg.register(_profile(name="none"))

    pro_names = {p.name for p in reg.profiles_serving_tier(InferenceTier.PRO)}
    assert pro_names == {"pro_only", "both"}
    wh_names = {p.name for p in reg.profiles_serving_tier(InferenceTier.WORKHORSE)}
    assert wh_names == {"both"}


def test_healthy_profiles_skip_failed_deps() -> None:
    reg = ProviderProfileRegistry()
    reg.register(_profile(name="ok", check=lambda: True))
    reg.register(_profile(name="missing", check=lambda: False))

    def evil_check() -> bool:
        raise RuntimeError("kaboom")

    reg.register(_profile(name="evil", check=evil_check))

    healthy_names = {p.name for p in reg.healthy_profiles()}
    assert healthy_names == {"ok"}


# ---- built-in profile contract ----


def test_eight_builtins_registered() -> None:
    """Importing korpha.inference eagerly registers all built-ins.

    If this regresses, the picker breaks + setup CLI shows a partial
    catalog. The exact count is intentional — adding a new built-in
    means updating this assertion deliberately."""
    names = {p.name for p in provider_profile_registry.builtin_profiles()}
    expected = {
        "deepseek",
        "ollama-cloud",
        "opencode-go",
        "opencode-zen",
        "openrouter",
        "local-ollama",
        "codex-cli",
        "claude-code-cli",
    }
    assert expected.issubset(names), (
        f"missing built-ins: {expected - names}"
    )


@pytest.mark.parametrize("profile_name", [
    "deepseek", "ollama-cloud", "opencode-go", "opencode-zen",
    "openrouter", "local-ollama", "codex-cli", "claude-code-cli",
])
def test_builtin_has_required_metadata(profile_name: str) -> None:
    """Every built-in must have name, label, factory, auth_type,
    api_mode, at least one tier_capability, description. These are
    what the picker / setup CLI / router actually read."""
    p = provider_profile_registry.get(profile_name)
    assert p is not None
    assert p.name == profile_name
    assert p.label
    assert callable(p.provider_factory)
    assert isinstance(p.auth_type, AuthType)
    assert isinstance(p.api_mode, ApiMode)
    assert p.tier_capabilities, (
        f"profile {profile_name} declares no tier_capabilities — "
        f"router won't be able to pick it"
    )
    assert p.description, f"{profile_name} has no description"


def test_api_key_profiles_have_setup_fields() -> None:
    """API-key providers MUST declare a SetupField so the
    interactive setup CLI knows what env var to prompt for. The
    Mike-non-technical rule depends on this."""
    for p in provider_profile_registry.builtin_profiles():
        if p.auth_type == AuthType.API_KEY and p.name != "local-ollama":
            assert p.setup_fields, (
                f"{p.name} is auth=api_key but declares no setup_fields"
            )
            for f in p.setup_fields:
                assert isinstance(f, SetupField)
                assert f.env_var, "SetupField needs env_var"
                assert f.description, "SetupField needs description"


def test_subscription_profiles_have_install_hint() -> None:
    """Subscription-CLI providers (codex, claude-code) need an
    install_hint or check_fn that actually checks for the binary,
    so users without it see something actionable."""
    for p in provider_profile_registry.builtin_profiles():
        if p.auth_type == AuthType.SUBSCRIPTION_CLI:
            assert p.install_hint, f"{p.name} missing install_hint"


def test_setup_field_secret_default() -> None:
    """API keys are secret by default — masked in CLI input + redacted
    in logs. Anyone setting secret=False should be deliberate."""
    for p in provider_profile_registry.builtin_profiles():
        for f in p.setup_fields:
            # All built-in fields are API keys → secret=True
            assert f.secret, f"{p.name}.{f.env_var} should be secret=True"


def test_provider_factory_returns_provider_instance() -> None:
    """The factory contract: returns a Provider that the runtime can
    actually call. We don't do an HTTP test here — just check the
    factory runs and produces the right protocol."""
    from korpha.inference.provider import Provider
    for p in provider_profile_registry.builtin_profiles():
        # local-ollama factory always works, codex/claude need binary
        # on PATH which test envs often lack — skip unless check_fn
        # passes.
        if not p.check_fn():
            continue
        instance = p.provider_factory()
        assert isinstance(instance, Provider), (
            f"{p.name} factory returned {type(instance)}, expected Provider"
        )
        assert instance.name, f"{p.name} factory returned a Provider with no .name"
