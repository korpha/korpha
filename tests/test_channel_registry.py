"""Tests for the channel adapter registry.

These cover the registry mechanics, not the adapter implementations
themselves (those have their own test modules). The point is to lock
down the contract plugins will rely on.
"""
from __future__ import annotations

from typing import Any

import pytest

from korpha.channels.registry import (
    PlatformEntry,
    PlatformRegistry,
)


def _entry(
    *,
    name: str = "test",
    factory: Any = None,
    check: Any = None,
    validate: Any = None,
    source: str = "plugin",
) -> PlatformEntry:
    return PlatformEntry(
        name=name,
        label=name.title(),
        adapter_factory=factory or (lambda cfg: object()),
        check_fn=check or (lambda: True),
        validate_config=validate,
        source=source,
    )


def test_register_and_get() -> None:
    reg = PlatformRegistry()
    entry = _entry(name="alpha")
    reg.register(entry)
    assert reg.is_registered("alpha")
    assert reg.get("alpha") is entry
    assert reg.get("missing") is None


def test_register_replaces_on_collision() -> None:
    reg = PlatformRegistry()
    first = _entry(name="x", source="builtin")
    second = _entry(name="x", source="plugin")
    reg.register(first)
    reg.register(second)
    assert reg.get("x") is second
    assert len(reg.all_entries()) == 1


def test_unregister() -> None:
    reg = PlatformRegistry()
    reg.register(_entry(name="bye"))
    assert reg.unregister("bye") is True
    assert reg.unregister("bye") is False
    assert reg.get("bye") is None


def test_create_adapter_returns_instance_when_check_passes() -> None:
    reg = PlatformRegistry()

    class Fake:
        def __init__(self, cfg: Any) -> None:
            self.cfg = cfg

    reg.register(_entry(name="ok", factory=Fake))
    adapter = reg.create_adapter("ok", {"x": 1})
    assert isinstance(adapter, Fake)
    assert adapter.cfg == {"x": 1}


def test_create_adapter_returns_none_for_unknown() -> None:
    reg = PlatformRegistry()
    assert reg.create_adapter("nope", {}) is None


def test_create_adapter_skips_when_deps_missing() -> None:
    reg = PlatformRegistry()
    reg.register(_entry(
        name="needs_dep",
        check=lambda: False,
    ))
    assert reg.create_adapter("needs_dep", {}) is None


def test_create_adapter_skips_when_config_invalid() -> None:
    reg = PlatformRegistry()
    reg.register(_entry(
        name="bad_cfg",
        validate=lambda cfg: bool(cfg.get("token")),
    ))
    assert reg.create_adapter("bad_cfg", {}) is None
    assert reg.create_adapter("bad_cfg", {"token": "x"}) is not None


def test_create_adapter_swallows_factory_errors() -> None:
    reg = PlatformRegistry()

    def boom(_cfg: Any) -> Any:
        raise RuntimeError("kaboom")

    reg.register(_entry(name="boom", factory=boom))
    # Factory exceptions should not bubble — return None so the
    # caller can decide between skip/surface. This matches Hermes.
    assert reg.create_adapter("boom", {}) is None


def test_validate_config_exception_treated_as_invalid() -> None:
    reg = PlatformRegistry()

    def evil_validate(_cfg: Any) -> bool:
        raise ValueError("validate raised")

    reg.register(_entry(name="evil", validate=evil_validate))
    assert reg.create_adapter("evil", {}) is None


def test_filtering_by_source() -> None:
    reg = PlatformRegistry()
    reg.register(_entry(name="b1", source="builtin"))
    reg.register(_entry(name="b2", source="builtin"))
    reg.register(_entry(name="p1", source="plugin"))

    builtins = {e.name for e in reg.builtin_entries()}
    plugins = {e.name for e in reg.plugin_entries()}
    assert builtins == {"b1", "b2"}
    assert plugins == {"p1"}


def test_module_singleton_has_builtins_registered() -> None:
    """Importing korpha.channels eagerly registers the built-ins.

    This is the contract the runtime depends on — if this regresses,
    Telegram + email stop being discoverable by name."""
    from korpha.channels import platform_registry

    names = {e.name for e in platform_registry.all_entries()}
    # We don't assert exact set because tests run in arbitrary order
    # and other test modules may register their own; just check the
    # baseline we ship.
    assert "telegram" in names
    assert "email" in names


def test_required_env_is_a_list() -> None:
    """plugin.yaml-style metadata; surfaced in `korpha doctor`."""
    e = _entry(name="env_test")
    e.required_env.append("FOO")
    e.required_env.append("BAR")
    assert e.required_env == ["FOO", "BAR"]
