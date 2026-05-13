"""Tests for the workspace checkpoint manager.

Snapshot/restore/prune cycle exercised end-to-end against a tmp
workspace. The destructive bits (tar-slip / symlink-escape
defenses) get explicit hostile-input tests since a bug there would
let a malicious snapshot write arbitrary files on restore.
"""
from __future__ import annotations

import os
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from korpha.checkpoints import (
    Checkpoint,
    CheckpointError,
    list_checkpoints,
    prune,
    restore,
    snapshot,
)
from korpha.checkpoints.manager import (
    _DEFAULT_EXCLUDES,
    _resolve_excludes,
    _walk_files,
)


@pytest.fixture(autouse=True)
def _pin_v1_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file exercises v1 (tar.gz) storage explicitly. Default
    is now v2; pin it to v1 here so the tarball assertions still
    fire. v2 has its own dedicated test file."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v1")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Build a small fake repo:

        repo/
          README.md
          src/
            main.py
          .git/             (excluded)
            HEAD
          node_modules/     (excluded)
            big.bin
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "node_modules").mkdir()
    (repo / "README.md").write_text("# Project\n")
    (repo / "src" / "main.py").write_text("print('hi')\n")
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "node_modules" / "big.bin").write_bytes(b"\x00" * 1024)
    return repo


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Per-workspace base_dir so tests don't share storage."""
    return tmp_path / "_checkpoint_store"


# ---- excludes ----


def test_default_excludes_include_common_junk() -> None:
    assert ".git" in _DEFAULT_EXCLUDES
    assert "node_modules" in _DEFAULT_EXCLUDES
    assert "__pycache__" in _DEFAULT_EXCLUDES
    assert ".venv" in _DEFAULT_EXCLUDES


def test_resolve_excludes_reads_workspace_ignore_file(
    workspace: Path,
) -> None:
    (workspace / ".korpha-checkpoint-ignore").write_text(
        "build\n# comment\nlogs\n  \n",
    )
    excludes = _resolve_excludes(workspace)
    assert "build" in excludes
    assert "logs" in excludes
    # Comment + blank line dropped
    assert "# comment" not in excludes
    # Defaults still present
    assert ".git" in excludes


def test_walk_files_skips_excluded_dirs(workspace: Path) -> None:
    files, total = _walk_files(workspace, _resolve_excludes(workspace))
    names = {f.name for f in files}
    assert "README.md" in names
    assert "main.py" in names
    # Excluded
    assert "HEAD" not in names
    assert "big.bin" not in names


def test_walk_files_raises_when_workspace_too_large(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock _MAX_SIZE_BYTES low to trigger the guard without writing
    actual gigabytes to tmp_path."""
    from korpha.checkpoints import manager as mgr
    monkeypatch.setattr(mgr, "_MAX_SIZE_BYTES", 100)
    with pytest.raises(CheckpointError, match="too large"):
        _walk_files(workspace, set())  # no excludes → includes node_modules


# ---- snapshot ----


def test_snapshot_writes_tarball_and_manifest(
    workspace: Path, store: Path,
) -> None:
    cp = snapshot(workspace, label="initial", base_dir=store)
    archive = store / f"{cp.id}.tar.gz"
    manifest = store / f"{cp.id}.json"
    assert archive.exists()
    assert manifest.exists()
    assert cp.label == "initial"
    assert cp.workspace_path == str(workspace.resolve())
    # Two non-excluded files in the fixture
    assert cp.file_count == 2


def test_snapshot_excludes_default_dirs(
    workspace: Path, store: Path,
) -> None:
    cp = snapshot(workspace, base_dir=store)
    archive = store / f"{cp.id}.tar.gz"
    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
    assert "README.md" in names
    assert "src/main.py" in names
    # .git and node_modules entries shouldn't be present
    assert not any(n.startswith(".git") for n in names)
    assert not any(n.startswith("node_modules") for n in names)


def test_snapshot_raises_for_missing_workspace(
    tmp_path: Path, store: Path,
) -> None:
    with pytest.raises(CheckpointError, match="not a directory"):
        snapshot(tmp_path / "missing", base_dir=store)


def test_snapshot_id_is_unique(
    workspace: Path, store: Path,
) -> None:
    a = snapshot(workspace, base_dir=store)
    b = snapshot(workspace, base_dir=store)
    assert a.id != b.id


# ---- list_checkpoints ----


def test_list_returns_empty_when_none(
    workspace: Path, store: Path,
) -> None:
    assert list_checkpoints(workspace, base_dir=store) == []


def test_list_returns_newest_first(
    workspace: Path, store: Path,
) -> None:
    import time
    a = snapshot(workspace, label="first", base_dir=store)
    time.sleep(1.01)  # ensure created_at sort differs
    b = snapshot(workspace, label="second", base_dir=store)
    cps = list_checkpoints(workspace, base_dir=store)
    assert [cp.id for cp in cps] == [b.id, a.id]


def test_list_skips_orphan_manifests(
    workspace: Path, store: Path,
) -> None:
    """A manifest without its tarball (manual deletion mid-prune)
    should be skipped, not surfaced."""
    cp = snapshot(workspace, base_dir=store)
    # Delete the tarball but leave the manifest
    (store / f"{cp.id}.tar.gz").unlink()
    assert list_checkpoints(workspace, base_dir=store) == []


def test_list_skips_corrupt_manifests(
    workspace: Path, store: Path,
) -> None:
    snapshot(workspace, base_dir=store)
    # Drop a corrupt JSON file alongside
    (store / "bogus.json").write_text("{not json")
    cps = list_checkpoints(workspace, base_dir=store)
    assert len(cps) == 1


# ---- restore ----


def test_restore_recovers_deleted_file(
    workspace: Path, store: Path,
) -> None:
    snap = snapshot(workspace, base_dir=store)
    # Modify the workspace
    (workspace / "src" / "main.py").unlink()
    (workspace / "src" / "new.py").write_text("# extra")
    # Restore
    restore(
        workspace, snap.id, base_dir=store, auto_pre_snapshot=False,
    )
    assert (workspace / "src" / "main.py").exists()
    assert (workspace / "src" / "main.py").read_text() == "print('hi')\n"


def test_restore_raises_for_unknown_id(
    workspace: Path, store: Path,
) -> None:
    with pytest.raises(CheckpointError, match="not found"):
        restore(workspace, "deadbeef0000", base_dir=store)


def test_restore_takes_pre_snapshot_by_default(
    workspace: Path, store: Path,
) -> None:
    """Auto pre-snapshot means even if the user restores by mistake,
    they can re-restore the prior state."""
    snap = snapshot(workspace, base_dir=store)
    # Mutate
    (workspace / "src" / "main.py").write_text("print('CHANGED')\n")
    pre = restore(workspace, snap.id, base_dir=store)
    # Two snapshots now: original + pre-restore
    cps = list_checkpoints(workspace, base_dir=store)
    assert len(cps) == 2
    assert any(cp.id == pre.id for cp in cps)
    # The pre-restore snapshot should contain the CHANGED content,
    # so re-restoring it returns us to the modified state.
    restore(workspace, pre.id, base_dir=store, auto_pre_snapshot=False)
    assert (workspace / "src" / "main.py").read_text() == "print('CHANGED')\n"


def test_restore_refuses_path_traversal_in_tarball(
    workspace: Path, store: Path,
) -> None:
    """Defense-in-depth: a hostile tarball with ``../etc/passwd``
    member must not write outside the workspace. The TarInfo check
    runs before extractall."""
    store.mkdir(parents=True, exist_ok=True)
    archive = store / "evil.tar.gz"
    # Hand-craft a tarball with a traversal member
    with tarfile.open(archive, "w:gz") as tar:
        bad = tarfile.TarInfo(name="../escape.txt")
        payload = b"i am outside the workspace"
        bad.size = len(payload)
        tar.addfile(bad, BytesIO(payload))
    # Need a manifest sidecar so list_checkpoints would find it,
    # but restore() doesn't read the manifest — just checks the
    # archive exists. Direct call:
    with pytest.raises(CheckpointError, match="unsafe member"):
        restore(
            workspace, "evil", base_dir=store, auto_pre_snapshot=False,
        )
    # And the file outside the workspace must not exist
    assert not (workspace.parent / "escape.txt").exists()


# ---- prune ----


def test_prune_drops_oldest_beyond_cap(
    workspace: Path, store: Path,
) -> None:
    import time
    snaps = []
    for i in range(5):
        snaps.append(snapshot(workspace, label=f"s{i}", base_dir=store))
        time.sleep(1.01)
    removed = prune(workspace, keep_last=2, base_dir=store)
    assert removed == 3
    cps = list_checkpoints(workspace, base_dir=store)
    assert len(cps) == 2
    # The two most recent (s3, s4) survive
    surviving = {cp.label for cp in cps}
    assert surviving == {"s3", "s4"}


def test_prune_under_cap_is_noop(
    workspace: Path, store: Path,
) -> None:
    snapshot(workspace, base_dir=store)
    removed = prune(workspace, keep_last=10, base_dir=store)
    assert removed == 0


# ---- code.ship_via_codex integration ----


@pytest.mark.asyncio
async def test_ship_via_codex_takes_pre_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: invoking the skill with a real cwd + non-readonly
    sandbox triggers a snapshot before Codex runs. The snapshot id
    surfaces in the SkillResult.payload."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("# original")

    import shutil

    from korpha.delegation import (
        DelegationRequest, DelegationResponse,
    )
    from korpha.skills import code_deploy
    from korpha.skills.types import SkillContext

    class _StubCli:
        def __init__(self, *_a, **_k) -> None:
            pass
        async def run(self, _request: DelegationRequest) -> DelegationResponse:
            return DelegationResponse(
                content="ok", raw_output="ok", cost_usd=0.0,
            )

    monkeypatch.setattr(code_deploy, "CodexCLI", _StubCli)
    monkeypatch.setattr(
        "korpha.skills.code_deploy.shutil.which",
        lambda _name: "/usr/bin/codex",
    )

    class _Bus:
        workspace_path = repo
    class _Founder:
        pass
    ctx = SkillContext(
        business=_Bus(),
        founder=_Founder(),
        session=None,
        cost_tracker=None,
        invoking_agent_role_id=None,
    )
    skill = code_deploy.ShipViaCodexSkill()
    result = await skill.run(
        ctx=ctx,
        args={"prompt": "refactor", "cwd": str(repo)},
    )
    snap_id = result.payload.get("pre_snapshot_id")
    assert snap_id is not None
    assert "checkpoints restore" in result.summary
    # And the snapshot is actually on disk under tmp_path
    cps = list_checkpoints(repo)
    assert any(cp.id == snap_id for cp in cps)


@pytest.mark.asyncio
async def test_ship_via_codex_skips_snapshot_in_read_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sandbox_mode='read-only' means Codex can't mutate, so the
    pre-snapshot would just burn disk. Skip it."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("# original")

    from korpha.delegation import (
        DelegationRequest, DelegationResponse,
    )
    from korpha.skills import code_deploy
    from korpha.skills.types import SkillContext

    class _StubCli:
        def __init__(self, *_a, **_k) -> None:
            pass
        async def run(self, _request: DelegationRequest) -> DelegationResponse:
            return DelegationResponse(
                content="ok", raw_output="ok", cost_usd=0.0,
            )

    monkeypatch.setattr(code_deploy, "CodexCLI", _StubCli)
    monkeypatch.setattr(
        "korpha.skills.code_deploy.shutil.which",
        lambda _name: "/usr/bin/codex",
    )

    class _Bus:
        workspace_path = repo
    class _Founder:
        pass
    ctx = SkillContext(
        business=_Bus(),
        founder=_Founder(),
        session=None, cost_tracker=None, invoking_agent_role_id=None,
    )
    skill = code_deploy.ShipViaCodexSkill()
    result = await skill.run(
        ctx=ctx,
        args={
            "prompt": "look only", "cwd": str(repo),
            "sandbox_mode": "read-only",
        },
    )
    assert result.payload.get("pre_snapshot_id") is None
