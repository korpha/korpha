"""Tests for the v2 content-addressed checkpoint store."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from korpha.checkpoints.manager import (
    CheckpointError, list_checkpoints, restore, snapshot,
)
from korpha.checkpoints.v2 import (
    _blob_path, _blobs_dir, _hash_file,
    disk_breakdown, restore_v2, snapshot_v2, vacuum,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A scratch workspace with a few files + a sub-tree."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("hi", encoding="utf-8")
    (ws / "data.json").write_text('{"x": 1}', encoding="utf-8")
    sub = ws / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# title\nbody", encoding="utf-8")
    return ws


@pytest.fixture
def checkpoints_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Pin the checkpoint store to tmp_path so tests don't write into
    ~/.korpha. The shared blob store lives at <root>/blobs/.

    Returns the *checkpoints root* itself (= ``<tmp>/checkpoints``)
    so tests can pass ``base_dir`` consistently with where ``vacuum()``
    walks for referenced manifests."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    root = tmp_path / "checkpoints"
    root.mkdir()
    return root


def _ws_dir(checkpoints_root: Path, workspace: Path) -> Path:
    """A per-workspace dir under the real checkpoints root. Using
    a child of ``_checkpoints_root()`` rather than a side directory
    means ``vacuum()`` finds the manifests when scanning."""
    d = checkpoints_root / "ws-snapshots"
    d.mkdir(exist_ok=True)
    return d


# ---- snapshot_v2 happy path ----


def test_snapshot_v2_creates_manifest_and_blobs(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot_v2(workspace, label="test", base_dir=base)
    assert cp.file_count == 3
    assert cp.size_bytes > 0
    # Manifest exists
    manifest = base / f"{cp.id}.v2.json"
    assert manifest.is_file()
    # Each file's blob lives under <root>/blobs/<2chars>/<hash>
    blobs = list(_blobs_dir().rglob("*"))
    assert len([b for b in blobs if b.is_file()]) == 3


def test_snapshot_v2_dedups_repeated_files(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two snapshots of the same files should write blobs only once."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    snapshot_v2(workspace, base_dir=base)
    blobs_after_first = sum(
        1 for p in _blobs_dir().rglob("*") if p.is_file()
    )
    snapshot_v2(workspace, base_dir=base)  # identical files
    blobs_after_second = sum(
        1 for p in _blobs_dir().rglob("*") if p.is_file()
    )
    assert blobs_after_first == blobs_after_second == 3


def test_snapshot_v2_partial_change_only_new_blobs(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modify one file → only one new blob added."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    snapshot_v2(workspace, base_dir=base)
    initial = sum(1 for p in _blobs_dir().rglob("*") if p.is_file())
    # change one file
    (workspace / "hello.txt").write_text("hi-changed", encoding="utf-8")
    snapshot_v2(workspace, base_dir=base)
    after = sum(1 for p in _blobs_dir().rglob("*") if p.is_file())
    assert after == initial + 1  # one new unique blob


def test_snapshot_v2_rejects_missing_workspace(
    checkpoints_root: Path,
) -> None:
    with pytest.raises(CheckpointError, match="not a directory"):
        snapshot_v2("/nonexistent/path/blah")


# ---- restore_v2 happy path ----


def test_restore_v2_roundtrip(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot_v2(workspace, base_dir=base)
    # Wreck the workspace
    (workspace / "hello.txt").write_text("CORRUPTED")
    (workspace / "sub" / "nested.md").unlink()
    count = restore_v2(workspace, cp.id, base_dir=base)
    assert count == 3
    assert (workspace / "hello.txt").read_text() == "hi"
    assert (workspace / "sub" / "nested.md").read_text() == "# title\nbody"


def test_restore_v2_preserves_mode(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    script = workspace / "run.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    os.chmod(script, 0o755)
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot_v2(workspace, base_dir=base)
    os.chmod(script, 0o644)  # change between snapshots
    restore_v2(workspace, cp.id, base_dir=base)
    assert (script.stat().st_mode & 0o777) == 0o755


def test_restore_v2_unknown_snapshot_raises(
    workspace: Path, checkpoints_root: Path,
) -> None:
    base = _ws_dir(checkpoints_root, workspace)
    base.mkdir(exist_ok=True)
    with pytest.raises(CheckpointError, match="not found"):
        restore_v2(workspace, "deadbeef", base_dir=base)


def test_restore_v2_refuses_path_traversal(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a manifest somehow had a malicious path, restore must
    refuse rather than write outside the workspace."""
    import json
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot_v2(workspace, base_dir=base)
    manifest_path = base / f"{cp.id}.v2.json"
    data = json.loads(manifest_path.read_text())
    data["files"].append({
        "path": "../escape.txt",
        "sha256": data["files"][0]["sha256"],
        "size_bytes": 2,
        "mode": 0o644,
    })
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(CheckpointError, match="unsafe member|escapes"):
        restore_v2(workspace, cp.id, base_dir=base)


# ---- snapshot() (public) routes to v2 by default ----


def test_public_snapshot_defaults_to_v2(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env override → new snapshots are v2."""
    monkeypatch.delenv("KORPHA_CHECKPOINT_FORMAT", raising=False)
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot(workspace, base_dir=base)
    # v2 manifest written, no tar.gz
    assert (base / f"{cp.id}.v2.json").is_file()
    assert not (base / f"{cp.id}.tar.gz").exists()


def test_public_snapshot_v1_via_env_override(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v1")
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot(workspace, base_dir=base)
    assert (base / f"{cp.id}.tar.gz").is_file()
    assert (base / f"{cp.id}.json").is_file()
    assert not (base / f"{cp.id}.v2.json").exists()


# ---- list_checkpoints + restore: mixed v1/v2 ----


def test_list_returns_both_formats(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _ws_dir(checkpoints_root, workspace)
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v1")
    cp_v1 = snapshot(workspace, label="v1 one", base_dir=base)
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    cp_v2 = snapshot(workspace, label="v2 one", base_dir=base)
    cps = list_checkpoints(workspace, base_dir=base)
    ids = {c.id for c in cps}
    assert cp_v1.id in ids
    assert cp_v2.id in ids


def test_restore_v2_via_public_api(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """public ``restore()`` works with v2 manifests + skips the tar
    extract path."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot(workspace, base_dir=base)
    (workspace / "hello.txt").write_text("CORRUPTED")
    restore(workspace, cp.id, base_dir=base, auto_pre_snapshot=False)
    assert (workspace / "hello.txt").read_text() == "hi"


def test_restore_v1_still_works(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing v1 tar.gz snapshots from before the upgrade must
    still be restorable through the same public API."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v1")
    base = _ws_dir(checkpoints_root, workspace)
    cp = snapshot(workspace, base_dir=base)
    (workspace / "hello.txt").write_text("CORRUPTED")
    # Switch default to v2 — but the existing snapshot is v1.
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    restore(workspace, cp.id, base_dir=base, auto_pre_snapshot=False)
    assert (workspace / "hello.txt").read_text() == "hi"


# ---- vacuum ----


def test_vacuum_deletes_orphan_blobs(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blob no manifest references gets reclaimed."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    snapshot(workspace, base_dir=base)
    # Add an orphan blob directly
    fake_hash = "f" * 64
    fake_blob = _blob_path(fake_hash)
    fake_blob.parent.mkdir(parents=True, exist_ok=True)
    fake_blob.write_bytes(b"orphan content")
    initial_count = sum(1 for p in _blobs_dir().rglob("*") if p.is_file())
    assert fake_blob.is_file()

    stats = vacuum()
    assert stats["blobs_deleted"] == 1
    assert stats["blobs_kept"] == initial_count - 1
    assert not fake_blob.exists()


def test_vacuum_sweeps_tmp_files(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A .tmp left by a crashed write should be reclaimed."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    snapshot(workspace, base_dir=base)
    blob_root = _blobs_dir()
    shard = next(p for p in blob_root.iterdir() if p.is_dir())
    tmp = shard / "abandoned.tmp"
    tmp.write_bytes(b"crashed-write")

    stats = vacuum()
    assert stats["tmp_swept"] == 1
    assert not tmp.exists()


def test_vacuum_keeps_referenced_blobs(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-blobs-referenced case: vacuum is a no-op."""
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    snapshot(workspace, base_dir=base)
    stats = vacuum()
    assert stats["blobs_deleted"] == 0
    assert stats["blobs_kept"] == 3


# ---- disk_breakdown ----


def test_disk_breakdown_includes_blobs_and_workspaces(
    workspace: Path, checkpoints_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    base = _ws_dir(checkpoints_root, workspace)
    snapshot(workspace, base_dir=base)
    snapshot(workspace, base_dir=base)
    bd = disk_breakdown()
    assert bd["blob_count"] == 3  # dedup
    assert bd["blob_bytes"] > 0
    assert any(w["v2_count"] == 2 for w in bd["workspaces"])


def test_disk_breakdown_empty_store_returns_zeros(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    bd = disk_breakdown()
    assert bd["blob_count"] == 0
    assert bd["total_bytes"] == 0


# ---- _hash_file ----


def test_hash_file_stable(workspace: Path) -> None:
    """Hashing the same file twice gives the same digest."""
    h1 = _hash_file(workspace / "hello.txt")
    h2 = _hash_file(workspace / "hello.txt")
    assert h1 == h2
    assert len(h1) == 64
