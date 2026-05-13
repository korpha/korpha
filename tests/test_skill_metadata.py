"""Tests for SkillSpec.platforms + SkillSpec.provenance.

Platform gating: a Mac-only skill (osascript flows) shouldn't appear
on the Linux VPS's CEO catalog, and even if a stale router prompt
picks one, the registry should refuse to run it instead of crashing.

Provenance flag: future curator + dashboard need to tell built-in
skills apart from agent-authored ones (curator only auto-archives
its own creations).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from korpha.skills.registry import SkillRegistry
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)


# ---- supports_current_platform ----


def test_empty_platforms_means_no_restriction() -> None:
    spec = SkillSpec(name="t", description="", platforms=())
    assert spec.supports_current_platform() is True


def test_current_platform_in_list() -> None:
    spec = SkillSpec(name="t", description="", platforms=(sys.platform,))
    assert spec.supports_current_platform() is True


def test_current_platform_not_in_list() -> None:
    other = "darwin" if sys.platform != "darwin" else "linux"
    spec = SkillSpec(name="t", description="", platforms=(other,))
    assert spec.supports_current_platform() is False


# ---- registry filters list_specs by platform ----


class _StubSkill(Skill):
    def __init__(self, spec: SkillSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> SkillSpec:
        return self._spec

    async def run(
        self, *, ctx: SkillContext, args: dict,
    ) -> SkillResult:
        return SkillResult(skill_name=self._spec.name, summary="", payload={})


def test_list_specs_excludes_unsupported_platform() -> None:
    other = "darwin" if sys.platform != "darwin" else "linux"
    reg = SkillRegistry()
    reg.add(_StubSkill(SkillSpec(name="ok", description="")))
    reg.add(_StubSkill(SkillSpec(
        name="mac_only", description="", platforms=(other,),
    )))
    names = {s.name for s in reg.list_specs()}
    assert "ok" in names
    assert "mac_only" not in names


def test_list_specs_include_unsupported_returns_all() -> None:
    """Diagnostic listings (doctor / dashboard admin) need the full
    set."""
    other = "darwin" if sys.platform != "darwin" else "linux"
    reg = SkillRegistry()
    reg.add(_StubSkill(SkillSpec(
        name="mac_only", description="", platforms=(other,),
    )))
    names = {
        s.name for s in reg.list_specs(include_unsupported=True)
    }
    assert "mac_only" in names


@pytest.mark.asyncio
async def test_run_refuses_unsupported_platform() -> None:
    """Defense in depth — even if the catalog leaks an OS-restricted
    skill, the registry refuses to actually invoke it."""
    other = "darwin" if sys.platform != "darwin" else "linux"
    reg = SkillRegistry()
    reg.add(_StubSkill(SkillSpec(
        name="mac_only", description="", platforms=(other,),
    )))
    with pytest.raises(SkillError, match="requires platform"):
        await reg.run("mac_only", ctx=None, args={})  # type: ignore[arg-type]


# ---- SkillProvenance ----


def test_provenance_enum_values() -> None:
    assert SkillProvenance.BUILTIN.value == "builtin"
    assert SkillProvenance.AGENT_AUTHORED.value == "agent_authored"
    assert SkillProvenance.USER_AUTHORED.value == "user_authored"


def test_default_provenance_is_builtin() -> None:
    spec = SkillSpec(name="t", description="")
    assert spec.provenance == SkillProvenance.BUILTIN


def test_explicit_agent_authored_provenance() -> None:
    spec = SkillSpec(
        name="agent.authored",
        description="",
        provenance=SkillProvenance.AGENT_AUTHORED,
    )
    assert spec.provenance == SkillProvenance.AGENT_AUTHORED


# ---- YAML loader honors platforms + provenance ----


def _write_yaml_skill(
    tmp_path: Path,
    *,
    name: str = "test.skill",
    extra: str = "",
) -> Path:
    skill_dir = tmp_path / name.replace(".", "_")
    skill_dir.mkdir()
    manifest = (
        f"name: {name}\n"
        f"description: a test skill\n"
        f"system_prompt: be helpful\n"
        f"user_prompt_template: do {{thing}}\n"
        f"parameters:\n  thing: what to do\n"
        f"output:\n  format: json\n  required_keys: []\n"
        f"{extra}"
    )
    (skill_dir / "manifest.yaml").write_text(manifest)
    return skill_dir


def test_yaml_loader_parses_platforms(tmp_path: Path) -> None:
    from korpha.skills.yaml_skill import load_yaml_skill

    skill_dir = _write_yaml_skill(
        tmp_path, extra="platforms: [darwin, linux]\n",
    )
    skill = load_yaml_skill(skill_dir)
    assert skill.spec.platforms == ("darwin", "linux")


def test_yaml_loader_rejects_unknown_platform(tmp_path: Path) -> None:
    from korpha.skills.yaml_skill import YamlSkillError, load_yaml_skill

    skill_dir = _write_yaml_skill(
        tmp_path, extra="platforms: [haiku-os]\n",
    )
    with pytest.raises(YamlSkillError, match="haiku-os"):
        load_yaml_skill(skill_dir)


def test_yaml_loader_default_provenance_is_user_authored(
    tmp_path: Path,
) -> None:
    """A YAML skill the founder hand-wrote is by default marked
    user_authored — the curator never touches these."""
    from korpha.skills.yaml_skill import load_yaml_skill

    skill_dir = _write_yaml_skill(tmp_path)
    skill = load_yaml_skill(skill_dir)
    assert skill.spec.provenance == SkillProvenance.USER_AUTHORED


def test_yaml_loader_parses_explicit_provenance(tmp_path: Path) -> None:
    from korpha.skills.yaml_skill import load_yaml_skill

    skill_dir = _write_yaml_skill(
        tmp_path, extra="provenance: agent_authored\n",
    )
    skill = load_yaml_skill(skill_dir)
    assert skill.spec.provenance == SkillProvenance.AGENT_AUTHORED


def test_yaml_loader_rejects_bad_provenance(tmp_path: Path) -> None:
    from korpha.skills.yaml_skill import YamlSkillError, load_yaml_skill

    skill_dir = _write_yaml_skill(
        tmp_path, extra="provenance: hand-of-god\n",
    )
    with pytest.raises(YamlSkillError, match="hand-of-god"):
        load_yaml_skill(skill_dir)


def test_yaml_loader_rejects_non_list_platforms(tmp_path: Path) -> None:
    from korpha.skills.yaml_skill import YamlSkillError, load_yaml_skill

    skill_dir = _write_yaml_skill(
        tmp_path, extra="platforms: 'linux'\n",
    )
    with pytest.raises(YamlSkillError, match="must be a list"):
        load_yaml_skill(skill_dir)


# ---- author_python_skill scaffold mentions provenance ----


def test_python_skill_scaffold_includes_provenance() -> None:
    """The template the LLM is given for drafting Python skills
    must include `provenance=SkillProvenance.AGENT_AUTHORED` so
    every generated skill gets the right tag automatically."""
    from korpha.skills.meta import _AUTHOR_PYTHON_PROMPT

    assert "AGENT_AUTHORED" in _AUTHOR_PYTHON_PROMPT
    assert "SkillProvenance" in _AUTHOR_PYTHON_PROMPT
