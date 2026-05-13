"""Tests for v1 → v2 checkpoint migration."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from korpha.checkpoints.manager import (
    list_checkpoints, restore, snapshot,
)
from korpha.checkpoints.v2 import (
    _blob_path, migrate_v1_to_v2,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    (ws / "b.txt").write_text("world", encoding="utf-8")
    return ws


@pytest.fixture
def base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Pin the checkpoints root to <tmp>/checkpoints + return a
    natural per-workspace dir under it so migrate_v1_to_v2 walks
    the right tree."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    root = tmp_path / "checkpoints"
    root.mkdir()
    ws_dir = root / "ws-snapshots"
    ws_dir.mkdir()
    return ws_dir


def _v1_snapshot(workspace: Path, base: Path, label: str = ""):
    """Always write a v1 tar.gz, regardless of the global default."""
    import os
    prev = os.environ.get("KORPHA_CHECKPOINT_FORMAT")
    os.environ["KORPHA_CHECKPOINT_FORMAT"] = "v1"
    try:
        return snapshot(workspace, label=label, base_dir=base)
    finally:
        if prev is None:
            del os.environ["KORPHA_CHECKPOINT_FORMAT"]
        else:
            os.environ["KORPHA_CHECKPOINT_FORMAT"] = prev


# ---- migrate_v1_to_v2 ----


def test_migrate_creates_v2_manifest_and_drops_v1(
    workspace: Path, base: Path,
) -> None:
    cp = _v1_snapshot(workspace, base, label="legacy")
    assert (base / f"{cp.id}.tar.gz").exists()
    stats = migrate_v1_to_v2()
    assert stats["migrated"] == 1
    assert stats["skipped"] == 0
    assert stats["failed"] == 0
    # v2 manifest written
    assert (base / f"{cp.id}.v2.json").exists()
    # v1 originals removed
    assert not (base / f"{cp.id}.tar.gz").exists()
    assert not (base / f"{cp.id}.json").exists()


def test_migrate_dry_run_keeps_originals(
    workspace: Path, base: Path,
) -> None:
    cp = _v1_snapshot(workspace, base, label="legacy")
    stats = migrate_v1_to_v2(delete_originals=False)
    assert stats["migrated"] == 1
    # v2 manifest written
    assert (base / f"{cp.id}.v2.json").exists()
    # v1 originals still present
    assert (base / f"{cp.id}.tar.gz").exists()
    assert (base / f"{cp.id}.json").exists()


def test_migrate_skips_already_v2(
    workspace: Path, base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run once → migrated. Run again → skipped (manifest exists)."""
    _v1_snapshot(workspace, base, label="legacy")
    migrate_v1_to_v2()
    stats = migrate_v1_to_v2()
    assert stats["migrated"] == 0
    # Nothing to skip either since v1 was deleted on first pass.
    # Add a fresh v1 snapshot and verify the existing v2 is left alone.
    cp2 = _v1_snapshot(workspace, base, label="legacy2")
    assert (base / f"{cp2.id}.tar.gz").exists()
    stats = migrate_v1_to_v2()
    assert stats["migrated"] == 1


def test_migrate_dedups_repeated_files_across_snapshots(
    workspace: Path, base: Path,
) -> None:
    """Two v1 snapshots of the same files should result in shared
    blobs after migration."""
    _v1_snapshot(workspace, base)
    _v1_snapshot(workspace, base)
    migrate_v1_to_v2()
    # One blob per unique file content (2 files), regardless of
    # how many snapshots reference them.
    blobs = [
        p for p in (base.parent / "blobs").rglob("*")
        if p.is_file() and not p.name.endswith(".tmp")
    ]
    assert len(blobs) == 2


def test_restore_works_after_migration(
    workspace: Path, base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrated snapshot should restore identically to the v1
    original. The contract is "you can migrate without losing any
    snapshot history."."""
    cp = _v1_snapshot(workspace, base, label="pre-migrate")
    migrate_v1_to_v2()
    # Wreck workspace
    (workspace / "a.txt").write_text("CORRUPTED")
    (workspace / "b.txt").unlink()
    # Restore via the v2 path
    monkeypatch.setenv("KORPHA_CHECKPOINT_FORMAT", "v2")
    restore(workspace, cp.id, base_dir=base, auto_pre_snapshot=False)
    assert (workspace / "a.txt").read_text() == "hello"
    assert (workspace / "b.txt").read_text() == "world"


def test_migrate_handles_no_v1_snapshots(
    base: Path,
) -> None:
    stats = migrate_v1_to_v2()
    assert stats["migrated"] == 0
    assert stats["skipped"] == 0
    assert stats["bytes_freed"] == 0


def test_list_after_migrate_shows_migrated_label(
    workspace: Path, base: Path,
) -> None:
    """Migrated snapshots without a v1 sidecar (rare, but possible)
    fall back to a "(migrated from v1)" label."""
    cp = _v1_snapshot(workspace, base, label="real-label")
    # Drop the sidecar so the migrate path falls through to the
    # default-label branch
    sidecar = base / f"{cp.id}.json"
    assert sidecar.is_file()
    sidecar.unlink()
    migrate_v1_to_v2()
    cps = list_checkpoints(workspace, base_dir=base)
    assert len(cps) == 1
    assert cps[0].label == "(migrated from v1)"


# ---- CLI ----


def test_cli_migrate(
    workspace: Path, base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`korpha checkpoints migrate` re-packs + reports."""
    _v1_snapshot(workspace, base, label="legacy")

    from korpha.cli import app
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["checkpoints", "migrate"])
    assert result.exit_code == 0, result.stdout
    assert "Migrated 1" in result.stdout
    assert "Reclaimed" in result.stdout


def test_cli_migrate_dry_run(
    workspace: Path, base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cp = _v1_snapshot(workspace, base, label="legacy")
    from korpha.cli import app
    cli_runner = CliRunner()
    result = cli_runner.invoke(
        app, ["checkpoints", "migrate", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "--dry-run" in result.stdout
    # tar.gz still exists
    assert (base / f"{cp.id}.tar.gz").exists()


def test_cli_migrate_nothing_to_do(
    base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korpha.cli import app
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["checkpoints", "migrate"])
    assert result.exit_code == 0
    assert "Nothing to migrate" in result.stdout
