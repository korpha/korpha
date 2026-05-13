"""Tests for meta.author_python_skill — v1.5 of skill self-extension.

Covers:
  - Envelope validation (forbidden patterns, AST checks, missing register)
  - End-to-end apply path: skill file lands on disk + class registers
  - Loader: previously-authored skills survive process restart
  - Skill catalog has both author skills (router can pick)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from korpha.skills import default_registry
from korpha.skills.meta import (
    _FORBIDDEN_PYTHON_FRAGMENTS,
    _validate_python_envelope,
)


# Canonical valid envelope used as a starting point for "edit one thing
# and see if it still passes" tests.
GOOD_SOURCE = """\
from typing import Any

from korpha.audit.model import InferenceTier
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillResult, SkillSpec,
)


class _Echo(Skill):
    spec = SkillSpec(
        name="test.echo",
        description="Echo a string back",
        parameters={"value": "string to echo"},
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        v = str(args.get("value") or "")
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"echoed {len(v)} chars",
            payload={"value": v},
            cost_usd=0.0,
        )


register(_Echo())
"""


def _envelope(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "test.echo",
        "description": "echo strings",
        "source": GOOD_SOURCE,
    }
    base.update(overrides)
    return base


# ---- shape validation ----


def test_validate_accepts_canonical_envelope() -> None:
    assert _validate_python_envelope(_envelope()) == []


def test_validate_rejects_empty_envelope() -> None:
    problems = _validate_python_envelope({})
    assert any("name" in p for p in problems)
    assert any("description" in p for p in problems)
    assert any("source" in p for p in problems)


def test_validate_rejects_bad_name_format() -> None:
    problems = _validate_python_envelope(_envelope(name="not dotted"))
    assert any("snake_case" in p for p in problems)


def test_validate_rejects_blank_description() -> None:
    problems = _validate_python_envelope(_envelope(description=""))
    assert any("description" in p for p in problems)


def test_validate_rejects_syntax_error() -> None:
    problems = _validate_python_envelope(
        _envelope(source="def broken(:\n    pass\n")
    )
    assert any("SyntaxError" in p for p in problems)


def test_validate_rejects_missing_skill_subclass() -> None:
    """Source has no class inheriting from Skill."""
    src = "from korpha.skills.registry import register\nregister(object())\n"
    problems = _validate_python_envelope(_envelope(source=src))
    assert any("Skill" in p and "no class" in p for p in problems)


def test_validate_rejects_missing_register_call() -> None:
    src = GOOD_SOURCE.replace("register(_Echo())\n", "")
    problems = _validate_python_envelope(_envelope(source=src))
    assert any("register(...)" in p for p in problems)


@pytest.mark.parametrize("fragment", [
    "import subprocess",
    "from subprocess import run",
    "import socket",
    "import urllib.request",
    "from requests import get",
    "eval(user_input)",
    "exec(payload)",
    "__import__('os')",
])
def test_validate_rejects_forbidden_fragments(fragment: str) -> None:
    """Any forbidden import/call kills the proposal at validation."""
    src = GOOD_SOURCE.replace(
        "from korpha.audit.model import InferenceTier",
        f"from korpha.audit.model import InferenceTier\n{fragment}",
    )
    problems = _validate_python_envelope(_envelope(source=src))
    assert any("forbidden pattern" in p for p in problems), (
        f"expected fragment {fragment!r} to be flagged; got: {problems}"
    )


def test_forbidden_fragments_list_covers_subprocess_socket_eval() -> None:
    """Spot-check the published list — if these slip out of the list,
    the regression test above won't help any more."""
    must_have = ("subprocess", "socket", "eval(", "exec(", "__import__")
    joined = "\n".join(_FORBIDDEN_PYTHON_FRAGMENTS)
    for needle in must_have:
        assert needle in joined, f"{needle!r} no longer in forbidden list"


# ---- apply path: file lands + module imports ----


def test_apply_path_writes_file_and_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: stage approval payload, call the apply function,
    verify the skill is in the registry and the file is on disk in
    the expected layout."""
    from uuid import uuid4

    from korpha.approvals.model import ActionClass, Approval
    from korpha.skills.meta import apply_python_skill_proposal_from_approval

    monkeypatch.setenv("HOME", str(tmp_path))

    # Build an approval row directly — this simulates what happens
    # after the LLM authoring + scan + stage steps that the
    # AuthorPythonSkillSkill runs in production.
    approval = Approval(
        business_id=uuid4(),
        agent_role_id=uuid4(),
        action_class=ActionClass.CODE_CHANGE,
        platform="meta",
        proposal_summary="test",
        action_payload={
            "kind": "author_python_skill",
            "skill_name": "test.apply_echo",
            "intent": "echo something for the test",
            "envelope": {"name": "test.apply_echo"},
            "source": GOOD_SOURCE.replace("test.echo", "test.apply_echo"),
            "manifest_yaml": "name: test.apply_echo\n",
            "scan": {"verdict": "safe", "summary": "", "findings": []},
            "trust_level": "agent-created",
        },
    )

    target = apply_python_skill_proposal_from_approval(approval)
    skill_file = target / "skill.py"
    assert skill_file.exists(), f"skill.py not written to {target}"
    # Manifest sibling is metadata-only but should also exist
    assert (target / "manifest.yaml").exists()

    # The module's register() call should have added the new skill to
    # the running default_registry.
    assert "test.apply_echo" in default_registry.skills


def test_apply_path_rejects_wrong_payload_kind() -> None:
    """Defensive: don't try to apply a YAML payload via the Python
    apply fn (or vice versa)."""
    from uuid import uuid4

    from korpha.approvals.model import ActionClass, Approval
    from korpha.skills.meta import apply_python_skill_proposal_from_approval

    approval = Approval(
        business_id=uuid4(),
        agent_role_id=uuid4(),
        action_class=ActionClass.CODE_CHANGE,
        platform="meta",
        proposal_summary="wrong",
        action_payload={
            "kind": "author_skill",  # wrong kind for the python apply fn
            "skill_name": "x",
            "manifest_yaml": "name: x",
        },
    )
    with pytest.raises(ValueError, match="not an author_python_skill"):
        apply_python_skill_proposal_from_approval(approval)


# ---- loader: process-restart survival ----


def test_loader_imports_previously_authored_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup loader walks the agent_created/python/ tree and
    importlib-loads each skill.py so re-starting the server doesn't
    drop authored skills."""
    from korpha.skills import load_agent_created_python_skills

    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))

    py_dir = tmp_path / "agent_created" / "python" / "test__loader_echo"
    py_dir.mkdir(parents=True)
    skill_src = GOOD_SOURCE.replace("test.echo", "test.loader_echo")
    (py_dir / "skill.py").write_text(skill_src, encoding="utf-8")

    loaded = load_agent_created_python_skills()
    assert any("loader_echo" in m for m in loaded)
    assert "test.loader_echo" in default_registry.skills


def test_loader_skips_dirs_without_skill_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory with no skill.py shouldn't crash the loader; it
    should silently skip and continue with siblings."""
    from korpha.skills import load_agent_created_python_skills

    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    (tmp_path / "agent_created" / "python" / "no_skill_here").mkdir(
        parents=True
    )
    # Sanity: should return empty list, not raise
    assert load_agent_created_python_skills() == []


def test_loader_isolates_one_bad_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken skill.py shouldn't stop other skills from loading."""
    from korpha.skills import load_agent_created_python_skills

    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    base = tmp_path / "agent_created" / "python"

    bad = base / "test__broken_skill"
    bad.mkdir(parents=True)
    (bad / "skill.py").write_text(
        "this is not valid python syntax", encoding="utf-8"
    )

    good = base / "test__working_skill"
    good.mkdir(parents=True)
    (good / "skill.py").write_text(
        GOOD_SOURCE.replace("test.echo", "test.working_skill_echo"),
        encoding="utf-8",
    )

    loaded = load_agent_created_python_skills()
    # The good one should still register even though the bad one failed
    assert "test.working_skill_echo" in default_registry.skills
    assert any("working_skill" in m for m in loaded)


# ---- registry: both author skills are picker-visible ----


def test_both_author_skills_registered() -> None:
    """The CEO router needs both names to be in the catalog so it can
    pick between them based on capability shape."""
    assert "meta.author_skill" in default_registry.skills
    assert "meta.author_python_skill" in default_registry.skills
