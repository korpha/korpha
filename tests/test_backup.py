"""Tests for the backup module — snapshot, full bundle, retention, restore.

Each test gets its own tmp_path data dir so they're hermetic — no
risk of clobbering the real ~/.korpha.
"""
from __future__ import annotations

import sqlite3
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from korpha.backup import (
    BackupKind,
    apply_retention,
    list_backups,
    restore_db_snapshot,
    take_db_snapshot,
    take_full_backup,
)
from korpha.backup.snapshot import RetentionPolicy


def _seed_db(data_dir: Path, marker: str = "hello") -> Path:
    """Create a tiny valid sqlite DB the snapshot tool can copy."""
    db = data_dir / "korpha.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    c.execute("INSERT INTO t (v) VALUES (?)", (marker,))
    c.commit(); c.close()
    return db


# ----- take_db_snapshot -----------------------------------------------------


def test_take_db_snapshot_creates_valid_sqlite(tmp_path: Path) -> None:
    _seed_db(tmp_path, "marker-A")
    info = take_db_snapshot(tmp_path)
    assert info.kind == BackupKind.DB_SNAPSHOT
    assert info.path.is_file()
    assert info.size_bytes > 0
    # The snapshot is a real, openable SQLite DB
    c = sqlite3.connect(info.path)
    rows = c.execute("SELECT v FROM t").fetchall()
    c.close()
    assert rows == [("marker-A",)]


def test_take_db_snapshot_no_db_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        take_db_snapshot(tmp_path)


def test_take_db_snapshot_under_concurrent_writes(tmp_path: Path) -> None:
    """sqlite3 backup is meant to be safe under in-flight writes —
    snapshot in the middle of a transaction should still produce
    a valid file (just may not include uncommitted rows)."""
    _seed_db(tmp_path, "row1")
    # Open a writer in the middle of a transaction
    writer = sqlite3.connect(tmp_path / "korpha.db")
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t (v) VALUES (?)", ("uncommitted",))
    info = take_db_snapshot(tmp_path)
    writer.rollback(); writer.close()
    c = sqlite3.connect(info.path)
    rows = [r[0] for r in c.execute("SELECT v FROM t").fetchall()]
    c.close()
    assert "row1" in rows
    # The uncommitted row should NOT be in the snapshot
    assert "uncommitted" not in rows


# ----- take_full_backup -----------------------------------------------------


def test_take_full_backup_includes_db_and_secrets(tmp_path: Path) -> None:
    _seed_db(tmp_path, "full-test")
    # seed a fake secrets dir
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "master.key").write_bytes(b"x" * 32)
    (tmp_path / "providers.yaml").write_text("providers: []\n")

    info = take_full_backup(tmp_path)
    assert info.kind == BackupKind.FULL_BUNDLE
    assert info.path.is_file()

    with tarfile.open(info.path) as tar:
        names = tar.getnames()
    assert "korpha.db" in names
    assert "secrets/master.key" in names
    assert "providers.yaml" in names


def test_take_full_backup_uses_consistent_db_snapshot(tmp_path: Path) -> None:
    """The DB inside the tar should be the snapshot, not the live
    file — so it's safe even mid-write."""
    _seed_db(tmp_path, "consistent")
    info = take_full_backup(tmp_path)
    # Extract the DB out of the tar and verify it's a valid SQLite
    extract = tmp_path / "extract"
    extract.mkdir()
    with tarfile.open(info.path) as tar:
        tar.extract("korpha.db", str(extract))  # noqa: S202
    c = sqlite3.connect(extract / "korpha.db")
    assert c.execute("SELECT v FROM t").fetchone() == ("consistent",)
    c.close()


# ----- list_backups ---------------------------------------------------------


def test_list_backups_newest_first(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    a = take_db_snapshot(tmp_path, ts="20260101T000000Z")
    b = take_db_snapshot(tmp_path, ts="20260102T000000Z")
    items = list_backups(tmp_path, kind=BackupKind.DB_SNAPSHOT)
    assert [x.filename for x in items] == [b.path.name, a.path.name]


def test_list_backups_filters_by_kind(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    take_db_snapshot(tmp_path)
    take_full_backup(tmp_path)
    snaps = list_backups(tmp_path, kind=BackupKind.DB_SNAPSHOT)
    bundles = list_backups(tmp_path, kind=BackupKind.FULL_BUNDLE)
    assert all(s.kind == BackupKind.DB_SNAPSHOT for s in snaps)
    assert all(s.kind == BackupKind.FULL_BUNDLE for s in bundles)
    assert len(snaps) >= 1
    assert len(bundles) == 1


def test_list_backups_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert list_backups(tmp_path) == []


# ----- retention (GFS) ------------------------------------------------------


def test_retention_keeps_recent_drops_old(tmp_path: Path) -> None:
    """Seed snapshots across a wide time range; verify only the
    GFS-selected ones survive."""
    _seed_db(tmp_path)
    # Create snapshots at known timestamps spanning a year
    now = datetime.now(tz=timezone.utc)
    ts_hours_ago = [0, 1, 2, 5, 25, 26, 49, 168, 169, 720, 8000]
    for h in ts_hours_ago:
        t = (now - timedelta(hours=h)).strftime("%Y%m%dT%H%M%SZ")
        take_db_snapshot(tmp_path, ts=t)
    before = len(list_backups(tmp_path, kind=BackupKind.DB_SNAPSHOT))
    assert before == len(ts_hours_ago)

    result = apply_retention(
        tmp_path,
        policy=RetentionPolicy(hourly=3, daily=3, weekly=2, monthly=2),
    )
    after = list_backups(tmp_path, kind=BackupKind.DB_SNAPSHOT)
    # Some kept, some deleted
    assert result["kept"] > 0
    assert result["deleted"] > 0
    assert result["kept"] + result["deleted"] == before
    assert len(after) == result["kept"]


def test_retention_no_op_when_under_quota(tmp_path: Path) -> None:
    """Few snapshots, all within hourly bucket → nothing deleted."""
    _seed_db(tmp_path)
    take_db_snapshot(tmp_path)
    result = apply_retention(tmp_path, policy=RetentionPolicy(
        hourly=24, daily=7, weekly=4, monthly=12,
    ))
    assert result["deleted"] == 0
    assert result["kept"] == 1


# ----- restore --------------------------------------------------------------


def test_restore_db_snapshot_replaces_live_db(tmp_path: Path) -> None:
    db = _seed_db(tmp_path, "original")
    snap = take_db_snapshot(tmp_path)
    # Mutate live DB
    c = sqlite3.connect(db)
    c.execute("UPDATE t SET v=?", ("mutated",))
    c.commit(); c.close()
    # Restore
    target = restore_db_snapshot(snap.filename, tmp_path)
    assert target == db
    c = sqlite3.connect(db)
    assert c.execute("SELECT v FROM t").fetchone() == ("original",)
    c.close()


def test_restore_db_snapshot_makes_safety_copy(tmp_path: Path) -> None:
    _seed_db(tmp_path, "before")
    snap = take_db_snapshot(tmp_path)
    restore_db_snapshot(snap.filename, tmp_path)
    safety = list(tmp_path.glob("korpha.db.before-restore.*"))
    assert len(safety) == 1


def test_restore_db_snapshot_accepts_partial_timestamp(tmp_path: Path) -> None:
    """Pass just the timestamp portion; the function finds it."""
    _seed_db(tmp_path, "ts-test")
    snap = take_db_snapshot(tmp_path, ts="20260512T120000Z")
    target = restore_db_snapshot("20260512T120000Z", tmp_path)
    assert target.exists()


def test_restore_db_snapshot_missing_raises(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    take_db_snapshot(tmp_path)
    with pytest.raises(FileNotFoundError):
        restore_db_snapshot("nonexistent.sqlite", tmp_path)


def test_restore_db_snapshot_skip_safety_copy(tmp_path: Path) -> None:
    _seed_db(tmp_path)
    snap = take_db_snapshot(tmp_path)
    restore_db_snapshot(
        snap.filename, tmp_path, create_safety_copy=False,
    )
    assert list(tmp_path.glob("korpha.db.before-restore.*")) == []
