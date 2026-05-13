"""Plugin loader + capability gate tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from korpha.heartbeats.dispatcher import HandlerRegistry
from korpha.plugins import (
    PluginHost,
    PluginLoadError,
    PluginPermissionError,
    discover_plugins,
    load_plugin,
)
from korpha.plugins.loader import parse_manifest
from korpha.skills.registry import SkillRegistry


def _host(manifest_perms: set[str], plugin_name: str = "test") -> PluginHost:
    return PluginHost(
        plugin_name=plugin_name,
        permissions=frozenset(manifest_perms),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )


def _write(tmp_path: Path, body: str, *, name: str = "demo") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "plugin.yaml").write_text(body, encoding="utf-8")
    return d


def test_parse_minimal_manifest(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: hello
version: 1.0.0
description: greets
author: t
entry_point: hello:register
permissions: [skills]
""",
    )
    m = parse_manifest(d / "plugin.yaml")
    assert m.name == "hello"
    assert m.permissions == frozenset({"skills"})
    assert m.entry_point == "hello:register"


def test_unknown_permission_errors(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: x
entry_point: x:register
permissions: [skills, network_root]
""",
    )
    with pytest.raises(PluginLoadError) as exc:
        parse_manifest(d / "plugin.yaml")
    assert "network_root" in str(exc.value)


def test_missing_entry_point_errors(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: x
permissions: []
""",
    )
    with pytest.raises(PluginLoadError):
        parse_manifest(d / "plugin.yaml")


def test_discover_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert discover_plugins(tmp_path / "missing") == []


def test_discover_skips_dirs_without_manifest(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
name: real
entry_point: x:register
permissions: []
""",
        name="real",
    )
    (tmp_path / "not-a-plugin").mkdir()
    found = discover_plugins(tmp_path)
    assert [m.name for m in found] == ["real"]


def test_load_file_entry_point_calls_register(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: filetest
entry_point: ./entry.py:register
permissions: [wakeup_handlers]
""",
        name="filetest",
    )
    (d / "entry.py").write_text(
        """
def register(host):
    async def handler(ctx):
        pass
    host.add_wakeup_handler("test.kind", handler)
""",
        encoding="utf-8",
    )
    manifest = parse_manifest(d / "plugin.yaml")
    host = _host({"wakeup_handlers"}, plugin_name="filetest")
    load_plugin(manifest, host)
    assert "test.kind" in host.contributed_handlers
    assert host.handler_registry.get("test.kind") is not None


def test_capability_gate_blocks_unauthorized_call(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: greedy
entry_point: ./entry.py:register
permissions: [wakeup_handlers]
""",
        name="greedy",
    )
    (d / "entry.py").write_text(
        """
from korpha.skills.types import Skill, SkillSpec, SkillContext, SkillResult


class _BadSkill(Skill):
    spec = SkillSpec(name="greedy.evil", description="should be blocked")

    async def run(self, *, ctx, args):
        return SkillResult(skill_name=self.spec.name, summary="x", payload={})


def register(host):
    host.add_skill(_BadSkill())
""",
        encoding="utf-8",
    )
    manifest = parse_manifest(d / "plugin.yaml")
    host = _host({"wakeup_handlers"}, plugin_name="greedy")
    with pytest.raises(PluginLoadError) as exc:
        load_plugin(manifest, host)
    # The PermissionError gets wrapped in PluginLoadError by load_plugin
    assert "skills" in str(exc.value).lower()


def test_capability_granted_when_declared(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: ok
entry_point: ./entry.py:register
permissions: [skills]
""",
        name="ok",
    )
    (d / "entry.py").write_text(
        """
from korpha.skills.types import Skill, SkillSpec, SkillResult


class _OkSkill(Skill):
    spec = SkillSpec(name="ok.x", description="ok")

    async def run(self, *, ctx, args):
        return SkillResult(skill_name=self.spec.name, summary="x", payload={})


def register(host):
    host.add_skill(_OkSkill())
""",
        encoding="utf-8",
    )
    manifest = parse_manifest(d / "plugin.yaml")
    host = _host({"skills"}, plugin_name="ok")
    load_plugin(manifest, host)
    assert host.contributed_skills == ["ok.x"]


def test_register_raising_is_wrapped(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: boom
entry_point: ./entry.py:register
permissions: []
""",
        name="boom",
    )
    (d / "entry.py").write_text(
        """
def register(host):
    raise RuntimeError("kaboom")
""",
        encoding="utf-8",
    )
    manifest = parse_manifest(d / "plugin.yaml")
    host = _host(set(), plugin_name="boom")
    with pytest.raises(PluginLoadError) as exc:
        load_plugin(manifest, host)
    assert "kaboom" in str(exc.value)


def test_invalid_entry_point_format_errors(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: bad
entry_point: just_a_name_no_colon
permissions: []
""",
        name="bad",
    )
    manifest = parse_manifest(d / "plugin.yaml")
    host = _host(set(), plugin_name="bad")
    with pytest.raises(PluginLoadError):
        load_plugin(manifest, host)


def test_missing_file_entry_point_errors(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        """
name: ghost
entry_point: ./nope.py:register
permissions: []
""",
        name="ghost",
    )
    manifest = parse_manifest(d / "plugin.yaml")
    host = _host(set(), plugin_name="ghost")
    with pytest.raises(PluginLoadError):
        load_plugin(manifest, host)


def test_repo_demo_plugin_loads() -> None:
    """The shipped example plugin must always parse and register cleanly."""
    here = Path(__file__).resolve().parent.parent
    plugin_dir = here / "examples" / "plugins" / "demo_plugin"
    manifest = parse_manifest(plugin_dir / "plugin.yaml")
    host = PluginHost(
        plugin_name=manifest.name,
        permissions=manifest.permissions,
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )
    load_plugin(manifest, host)
    assert "demo.tick" in host.contributed_handlers


def test_has_permission_helper() -> None:
    h = _host({"skills"})
    assert h.has_permission("skills") is True
    assert h.has_permission("mcp_servers") is False


def test_permission_error_lists_declared_perms() -> None:
    h = _host({"skills"}, plugin_name="x")
    with pytest.raises(PluginPermissionError) as exc:
        h.add_wakeup_handler("k", lambda ctx: None)  # type: ignore[arg-type]
    assert "skills" in str(exc.value)
    assert "wakeup_handlers" in str(exc.value)
