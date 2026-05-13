"""Tests for the skill curator.

Covers:
  - SkillUsage round-trip (to_dict / from_dict)
  - load_usage / save_usage atomicity + corruption recovery
  - record_invocation bumps + persists
  - install_usage_hook is idempotent
  - find_stale respects provenance, grace, min_uses, pinned
  - archive_skill: success path, refusal for non-agent-authored,
    cleanup of source dir + usage record
  - list_archived / restore_archived round-trip
  - pin / unpin
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from korpha.skills.curator import (
    DEFAULT_GRACE_DAYS,
    DEFAULT_STALE_AFTER_DAYS,
    SkillUsage,
    archive_skill,
    find_stale,
    install_usage_hook,
    list_archived,
    load_usage,
    pin_skill,
    record_invocation,
    restore_archived,
    save_usage,
    unpin_skill,
)
from korpha.skills.registry import SkillRegistry
from korpha.skills.types import (
    Skill, SkillContext, SkillProvenance, SkillResult, SkillSpec,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pin the curator's filesystem to tmp_path so tests can't
    smear into a real ~/.korpha."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path / "skills"))
    yield


# ---- SkillUsage / sidecar ----


def test_skill_usage_round_trips() -> None:
    u = SkillUsage(
        skill_name="x", use_count=4, last_invoked_at=12345.0,
        first_seen_at=11111.0, pinned=True,
    )
    again = SkillUsage.from_dict(u.to_dict())
    assert again == u


def test_load_usage_returns_empty_when_missing() -> None:
    assert load_usage() == {}


def test_save_then_load_round_trips() -> None:
    save_usage({"x": SkillUsage(skill_name="x", use_count=2)})
    out = load_usage()
    assert out["x"].use_count == 2


def test_load_recovers_from_corrupt_sidecar(tmp_path: Path) -> None:
    """A truncated JSON file shouldn't crash startup — return empty
    and let usage rebuild from there."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "_usage.json").write_text("{not json")
    assert load_usage() == {}


def test_save_atomic_no_tmp_left() -> None:
    save_usage({"x": SkillUsage(skill_name="x")})
    skills = Path(
        # Same default path the curator computes
    )
    import os
    base = os.environ["KORPHA_SKILLS_DIR"]
    files = sorted(p.name for p in Path(base).iterdir())
    # Only the sidecar; no .tmp left behind
    assert "_usage.json" in files
    assert not any(f.endswith(".tmp") for f in files)


# ---- record_invocation ----


def test_record_invocation_bumps_and_persists() -> None:
    record_invocation("niche.find")
    record_invocation("niche.find")
    record_invocation("landing.draft")
    usage = load_usage()
    assert usage["niche.find"].use_count == 2
    assert usage["landing.draft"].use_count == 1
    # last_invoked_at is recent
    assert usage["niche.find"].last_invoked_at > time.time() - 5


# ---- install_usage_hook ----


@pytest.mark.asyncio
async def test_install_usage_hook_is_idempotent() -> None:
    """Calling install twice should leave only one listener."""
    from korpha.plugins.hooks import HookKind, hook_registry

    hook_registry.clear()
    install_usage_hook()
    install_usage_hook()
    install_usage_hook()
    listeners = hook_registry.listeners(HookKind.POST_SKILL_CALL)
    curator_listeners = [
        n for n, _ in listeners if n == "_curator_usage"
    ]
    assert len(curator_listeners) == 1
    hook_registry.clear()


@pytest.mark.asyncio
async def test_post_skill_hook_records_successful_invocation() -> None:
    """End-to-end: a skill runs, the hook fires, usage is bumped."""
    from korpha.plugins.hooks import HookKind, hook_registry

    hook_registry.clear()
    install_usage_hook()

    class _Stub(Skill):
        spec = SkillSpec(name="curator.test", description="t")
        async def run(self, *, ctx, args):
            return SkillResult(
                skill_name="curator.test", summary="", payload={},
            )

    reg = SkillRegistry()
    reg.add(_Stub())
    ctx = SkillContext(
        business=type("B", (), {})(),
        founder=type("F", (), {})(),
        session=None, cost_tracker=None,
    )
    await reg.run("curator.test", ctx=ctx, args={})

    usage = load_usage()
    assert "curator.test" in usage
    assert usage["curator.test"].use_count == 1
    hook_registry.clear()


@pytest.mark.asyncio
async def test_failed_skill_does_not_bump_usage() -> None:
    """Failed runs shouldn't count as 'this skill is useful' — keeps
    the curator's signal clean."""
    from korpha.plugins.hooks import HookKind, hook_registry
    from korpha.skills.types import SkillError

    hook_registry.clear()
    install_usage_hook()

    class _Bad(Skill):
        spec = SkillSpec(name="curator.bad", description="t")
        async def run(self, *, ctx, args):
            raise SkillError("nope")

    reg = SkillRegistry()
    reg.add(_Bad())
    ctx = SkillContext(
        business=type("B", (), {})(),
        founder=type("F", (), {})(),
        session=None, cost_tracker=None,
    )
    with pytest.raises(SkillError):
        await reg.run("curator.bad", ctx=ctx, args={})

    usage = load_usage()
    assert "curator.bad" not in usage
    hook_registry.clear()


# ---- find_stale ----


def _make_skill(spec: SkillSpec) -> Skill:
    """Build a Skill subclass with the given spec inline so the
    Skill metaclass's spec-required check doesn't blow up."""
    spec_local = spec

    class _S(Skill):
        spec = spec_local

        async def run(self, *, ctx, args):
            return SkillResult(
                skill_name=spec_local.name, summary="", payload={},
            )

    return _S()


def _make_registry_with(spec: SkillSpec) -> SkillRegistry:
    reg = SkillRegistry()
    reg.add(_make_skill(spec))
    return reg


def test_find_stale_ignores_builtin_skills() -> None:
    """The curator never touches built-ins — those are the
    distribution's responsibility."""
    reg = _make_registry_with(SkillSpec(
        name="builtin.x", description="t",
        provenance=SkillProvenance.BUILTIN,
    ))
    # Even with zero usage and ancient first_seen, builtin is skipped
    save_usage({
        "builtin.x": SkillUsage(
            skill_name="builtin.x",
            first_seen_at=time.time() - 999 * 86400,
        ),
    })
    assert find_stale(registry=reg) == []


def test_find_stale_ignores_user_authored_skills() -> None:
    reg = _make_registry_with(SkillSpec(
        name="user.x", description="t",
        provenance=SkillProvenance.USER_AUTHORED,
    ))
    save_usage({
        "user.x": SkillUsage(
            skill_name="user.x",
            first_seen_at=time.time() - 999 * 86400,
        ),
    })
    assert find_stale(registry=reg) == []


def test_find_stale_respects_grace_period() -> None:
    """A brand-new agent-authored skill within the grace window
    should NOT be flagged as stale, even with zero uses."""
    reg = _make_registry_with(SkillSpec(
        name="agent.fresh", description="t",
        provenance=SkillProvenance.AGENT_AUTHORED,
    ))
    save_usage({
        "agent.fresh": SkillUsage(
            skill_name="agent.fresh",
            first_seen_at=time.time() - 1 * 86400,  # 1 day old
        ),
    })
    assert find_stale(registry=reg) == []


def test_find_stale_flags_old_unused_agent_skill() -> None:
    reg = _make_registry_with(SkillSpec(
        name="agent.old", description="t",
        provenance=SkillProvenance.AGENT_AUTHORED,
    ))
    save_usage({
        "agent.old": SkillUsage(
            skill_name="agent.old",
            use_count=0,
            first_seen_at=time.time() - 60 * 86400,  # 60 days old
            last_invoked_at=0,  # never used
        ),
    })
    cands = find_stale(registry=reg)
    assert len(cands) == 1
    assert cands[0].skill_name == "agent.old"


def test_find_stale_skips_pinned_skills() -> None:
    reg = _make_registry_with(SkillSpec(
        name="agent.fav", description="t",
        provenance=SkillProvenance.AGENT_AUTHORED,
    ))
    save_usage({
        "agent.fav": SkillUsage(
            skill_name="agent.fav",
            use_count=0,
            first_seen_at=time.time() - 60 * 86400,
            pinned=True,
        ),
    })
    assert find_stale(registry=reg) == []


def test_find_stale_skips_recently_used_skill() -> None:
    """Used 5 days ago, plenty above min_uses → not stale."""
    reg = _make_registry_with(SkillSpec(
        name="agent.active", description="t",
        provenance=SkillProvenance.AGENT_AUTHORED,
    ))
    save_usage({
        "agent.active": SkillUsage(
            skill_name="agent.active",
            use_count=10,
            first_seen_at=time.time() - 60 * 86400,
            last_invoked_at=time.time() - 5 * 86400,
        ),
    })
    assert find_stale(registry=reg) == []


def test_find_stale_sorted_by_oldest_use() -> None:
    """Sort makes the dry-run output show the most archive-worthy
    candidate at the top."""
    from korpha.skills.registry import SkillRegistry

    reg = SkillRegistry()
    for name in ("a", "b", "c"):
        reg.add(_make_skill(SkillSpec(
            name=name, description="t",
            provenance=SkillProvenance.AGENT_AUTHORED,
        )))

    now = time.time()
    save_usage({
        "a": SkillUsage(
            skill_name="a", use_count=0,
            first_seen_at=now - 60 * 86400,
            last_invoked_at=now - 50 * 86400,
        ),
        "b": SkillUsage(
            skill_name="b", use_count=0,
            first_seen_at=now - 60 * 86400,
            last_invoked_at=now - 90 * 86400,  # oldest
        ),
        "c": SkillUsage(
            skill_name="c", use_count=0,
            first_seen_at=now - 60 * 86400,
            last_invoked_at=now - 40 * 86400,
        ),
    })
    cands = find_stale(registry=reg)
    assert [c.skill_name for c in cands] == ["b", "a", "c"]


# ---- archive_skill / restore ----


def _build_archived_skill_dir(
    tmp_path: Path, skill_name: str, body: str = "stub",
) -> Path:
    """Lay down an agent_created/python/<safe>/skill.py so
    archive_skill has source to tar up."""
    safe = skill_name.replace(".", "_")
    target = tmp_path / "skills" / "agent_created" / "python" / safe
    target.mkdir(parents=True, exist_ok=True)
    (target / "skill.py").write_text(body)
    return target


def test_archive_skill_refuses_non_agent_authored(tmp_path: Path) -> None:
    """Builtin / user-authored skills can't be archived even if the
    founder asks. Use unregister or provenance change instead."""
    from korpha.skills.registry import default_registry

    skill = _make_skill(SkillSpec(
        name="builtin.target", description="t",
        provenance=SkillProvenance.BUILTIN,
    ))
    default_registry.skills.pop("builtin.target", None)
    default_registry.add(skill)
    try:
        assert archive_skill("builtin.target") is None
        assert "builtin.target" in default_registry.skills
    finally:
        default_registry.skills.pop("builtin.target", None)


def test_archive_skill_returns_none_for_unknown(tmp_path: Path) -> None:
    assert archive_skill("does.not.exist") is None


def test_archive_skill_round_trip(tmp_path: Path) -> None:
    """End-to-end: lay down a fake agent-authored skill, register
    it, archive it, confirm source removed + tarball exists +
    registry entry gone + usage record gone."""
    from korpha.skills.registry import default_registry

    src = _build_archived_skill_dir(
        tmp_path, "agent.todelete",
        body="# would be a real skill",
    )

    skill = _make_skill(SkillSpec(
        name="agent.todelete", description="t",
        provenance=SkillProvenance.AGENT_AUTHORED,
    ))
    default_registry.skills.pop("agent.todelete", None)
    default_registry.add(skill)
    save_usage({
        "agent.todelete": SkillUsage(
            skill_name="agent.todelete", use_count=2,
        ),
    })

    archive_path = archive_skill("agent.todelete")
    try:
        assert archive_path is not None
        assert archive_path.exists()
        # Source gone
        assert not src.exists()
        # Registry entry gone
        assert "agent.todelete" not in default_registry.skills
        # Usage record gone (so a future restore starts fresh)
        usage = load_usage()
        assert "agent.todelete" not in usage
    finally:
        default_registry.skills.pop("agent.todelete", None)


def test_list_archived_returns_newest_first(tmp_path: Path) -> None:
    archived_dir = tmp_path / "skills" / "archived"
    archived_dir.mkdir(parents=True)
    a = archived_dir / "a-100.tar.gz"
    a.write_bytes(b"x")
    b = archived_dir / "b-200.tar.gz"
    b.write_bytes(b"x")
    import os
    os.utime(a, (100, 100))
    os.utime(b, (200, 200))
    rows = list_archived()
    assert [r.name for r in rows] == ["b-200.tar.gz", "a-100.tar.gz"]


def test_list_archived_empty_when_no_dir(tmp_path: Path) -> None:
    assert list_archived() == []


# ---- pin / unpin ----


def test_pin_creates_record_if_missing() -> None:
    pin_skill("agent.fav")
    usage = load_usage()
    assert usage["agent.fav"].pinned is True


def test_unpin_returns_false_for_unknown() -> None:
    assert unpin_skill("nothing.here") is False


def test_unpin_clears_pinned_flag() -> None:
    pin_skill("agent.x")
    assert unpin_skill("agent.x") is True
    usage = load_usage()
    assert usage["agent.x"].pinned is False
