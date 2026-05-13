"""Local rotating snapshots — Layer 1 of the backup system.

Two artifact types:

* **DB snapshots**: ``sqlite3.backup`` of the live DB to
  ``<data_dir>/backups/db/db-<ts>.sqlite``. Atomic, lock-safe,
  takes a consistent point-in-time copy even while the app is
  writing. Hourly cadence by default.

* **Full bundles**: tar.gz of the entire data dir (DB + secrets +
  skills + cron-scripts + providers.yaml + …) to
  ``<data_dir>/backups/full/full-<date>.tar.gz``. Daily cadence
  by default. This is what you'd restore onto a new machine.

GFS retention (Grandfather-Father-Son): keep the last N hourly,
daily, weekly, monthly tiers. Configurable; defaults are sized so
the backup dir stays under ~1 GB for a typical Mike.

Failure semantics: every operation returns a structured result; we
log + continue on a single-snapshot failure so the cron preset
never breaks the line just because the disk is momentarily full.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class BackupKind(StrEnum):
    DB_SNAPSHOT = "db_snapshot"
    FULL_BUNDLE = "full_bundle"


@dataclass(frozen=True)
class BackupInfo:
    """Metadata for one backup artifact on disk."""

    kind: BackupKind
    path: Path
    size_bytes: int
    created_at: datetime
    """UTC timestamp parsed from the filename."""

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def age(self) -> timedelta:
        return datetime.now(tz=timezone.utc) - self.created_at


# Directories that should be included in the full tar.gz bundle.
# Logs are skipped — they can be regenerated and dominate disk
# usage. The DB itself is included so a full restore is one step.
_FULL_INCLUDE_DIRS = (
    "secrets",
    "skills",
    "cron-scripts",
    "checkpoints",
    "calendar",
    "deploys",
    "providers.yaml",
)

# Top-level files always included
_FULL_INCLUDE_FILES = (
    "korpha.db",
    "providers.yaml",
)


def _data_root(data_dir: Path | str | None = None) -> Path:
    """Default to the env / user home if not explicitly passed.

    Mirrors korpha.secrets.store._data_root semantics so dev +
    live instances both work.
    """
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    env = os.environ.get("KORPHA_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".korpha").resolve()


def _backups_root(data_dir: Path | str | None = None) -> Path:
    return _data_root(data_dir) / "backups"


def _ts() -> str:
    """Filesystem-safe UTC timestamp suffix (sorts naturally)."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_ts(name: str) -> datetime | None:
    """Pull the ``%Y%m%dT%H%M%SZ`` portion out of a backup filename."""
    for chunk in name.replace(".", "-").split("-"):
        if len(chunk) == 16 and chunk.endswith("Z") and "T" in chunk:
            try:
                return datetime.strptime(
                    chunk, "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# DB snapshot
# ---------------------------------------------------------------------------


def take_db_snapshot(
    data_dir: Path | str | None = None,
    *,
    ts: str | None = None,
) -> BackupInfo:
    """Atomic point-in-time copy of korpha.db.

    Uses sqlite's built-in ``backup`` API — safe to call against a
    DB currently being written to, no service downtime, no
    corruption risk. The output file is a fully-valid standalone
    SQLite DB.
    """
    root = _data_root(data_dir)
    src = root / "korpha.db"
    if not src.is_file():
        raise FileNotFoundError(f"no korpha.db at {src}")

    dst_dir = _backups_root(data_dir) / "db"
    dst_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts or _ts()
    dst = dst_dir / f"db-{stamp}.sqlite"

    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    info = BackupInfo(
        kind=BackupKind.DB_SNAPSHOT,
        path=dst,
        size_bytes=dst.stat().st_size,
        created_at=_parse_ts(dst.name) or datetime.now(tz=timezone.utc),
    )
    logger.info(
        "db snapshot taken: %s (%d bytes)", dst.name, info.size_bytes,
    )
    return info


# ---------------------------------------------------------------------------
# Full bundle
# ---------------------------------------------------------------------------


def take_full_backup(
    data_dir: Path | str | None = None,
    *,
    ts: str | None = None,
) -> BackupInfo:
    """tar.gz of the entire data dir minus logs.

    This is the "I just bought a new laptop" artifact — extract it
    to ``~/.korpha/`` and the install resumes exactly where it
    left off (assuming you also brought the master.key, which IS
    in here)."""
    root = _data_root(data_dir)
    dst_dir = _backups_root(data_dir) / "full"
    dst_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts or _ts()
    dst = dst_dir / f"full-{stamp}.tar.gz"

    # First, take a fresh DB snapshot so the included korpha.db
    # is point-in-time consistent (vs. mid-write).
    snap = take_db_snapshot(data_dir, ts=stamp)

    with tarfile.open(dst, "w:gz") as tar:
        # Add the consistent DB snapshot under its real name
        tar.add(str(snap.path), arcname="korpha.db")
        for name in _FULL_INCLUDE_DIRS:
            src = root / name
            if src.exists():
                tar.add(str(src), arcname=name)
        for name in _FULL_INCLUDE_FILES:
            if name == "korpha.db":
                continue  # already added via snapshot
            src = root / name
            if src.is_file():
                tar.add(str(src), arcname=name)

    info = BackupInfo(
        kind=BackupKind.FULL_BUNDLE,
        path=dst,
        size_bytes=dst.stat().st_size,
        created_at=_parse_ts(dst.name) or datetime.now(tz=timezone.utc),
    )
    logger.info(
        "full bundle taken: %s (%d bytes)", dst.name, info.size_bytes,
    )
    return info


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_backups(
    data_dir: Path | str | None = None,
    *,
    kind: BackupKind | None = None,
) -> list[BackupInfo]:
    """All backups under <data_dir>/backups, newest first.

    Filter by kind if you only want snapshots or only bundles."""
    out: list[BackupInfo] = []
    root = _backups_root(data_dir)
    pairs = [
        (BackupKind.DB_SNAPSHOT, root / "db", "db-", ".sqlite"),
        (BackupKind.FULL_BUNDLE, root / "full", "full-", ".tar.gz"),
    ]
    for k, d, prefix, suffix in pairs:
        if kind is not None and kind != k:
            continue
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_file() or not p.name.startswith(prefix):
                continue
            if not p.name.endswith(suffix):
                continue
            ts = _parse_ts(p.name)
            if ts is None:
                continue
            out.append(BackupInfo(
                kind=k, path=p, size_bytes=p.stat().st_size,
                created_at=ts,
            ))
    out.sort(key=lambda b: b.created_at, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Retention (GFS — Grandfather/Father/Son)
# ---------------------------------------------------------------------------


@dataclass
class RetentionPolicy:
    """How many of each tier to keep.

    Defaults targeted at a typical Mike — total disk usage under
    ~1 GB even for a busy DB."""

    hourly: int = 24      # last 24 hours
    daily: int = 7        # last week
    weekly: int = 4       # last month
    monthly: int = 12     # last year

    def keep_for(self, ages: list[timedelta]) -> set[int]:
        """Decide which indices in the (newest-first) age list to keep.

        Iterative bucket: for each tier, pick the newest backup
        within each bucket (hour / day / week / month) up to the
        tier's quota. Older backups outside every bucket get
        dropped.
        """
        keep: set[int] = set()
        # Hourly: keep the latest in each of the last N hours
        for h in range(self.hourly):
            for i, age in enumerate(ages):
                if (h * 3600) <= age.total_seconds() < ((h + 1) * 3600):
                    keep.add(i)
                    break
        # Daily: latest in each of the last N days (skipping first day handled by hourly)
        for d in range(self.daily):
            day_start = d * 86400
            day_end = (d + 1) * 86400
            for i, age in enumerate(ages):
                if day_start <= age.total_seconds() < day_end:
                    keep.add(i)
                    break
        # Weekly
        for w in range(self.weekly):
            week_start = w * 7 * 86400
            week_end = (w + 1) * 7 * 86400
            for i, age in enumerate(ages):
                if week_start <= age.total_seconds() < week_end:
                    keep.add(i)
                    break
        # Monthly (30-day approx)
        for m in range(self.monthly):
            mo_start = m * 30 * 86400
            mo_end = (m + 1) * 30 * 86400
            for i, age in enumerate(ages):
                if mo_start <= age.total_seconds() < mo_end:
                    keep.add(i)
                    break
        return keep


def apply_retention(
    data_dir: Path | str | None = None,
    *,
    policy: RetentionPolicy | None = None,
) -> dict[str, int]:
    """Apply GFS retention to both db snapshots and full bundles.

    Returns a dict ``{"kept": N, "deleted": M}`` so the cron preset
    can report meaningfully."""
    policy = policy or RetentionPolicy()
    kept_total = 0
    deleted_total = 0
    for kind in (BackupKind.DB_SNAPSHOT, BackupKind.FULL_BUNDLE):
        items = list_backups(data_dir, kind=kind)
        if not items:
            continue
        ages = [b.age for b in items]
        keep_idx = policy.keep_for(ages)
        for i, b in enumerate(items):
            if i in keep_idx:
                kept_total += 1
            else:
                try:
                    b.path.unlink()
                    deleted_total += 1
                except OSError as exc:
                    logger.warning(
                        "retention: failed to delete %s: %s",
                        b.path, exc,
                    )
    return {"kept": kept_total, "deleted": deleted_total}


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_db_snapshot(
    snapshot_name: str,
    data_dir: Path | str | None = None,
    *,
    create_safety_copy: bool = True,
) -> Path:
    """Replace the live korpha.db with the named snapshot.

    Stops short of touching the running server — caller's
    responsibility to stop + restart the server around this. The
    snapshot file is copied (not moved) so it stays in the backup
    set for repeat restores.

    A safety copy of the pre-restore DB is made by default so a
    bad restore is itself recoverable.
    """
    root = _data_root(data_dir)
    snap = _backups_root(data_dir) / "db" / snapshot_name
    if not snap.is_file():
        # Allow passing just the timestamp portion
        candidates = list(
            (_backups_root(data_dir) / "db").glob(f"*{snapshot_name}*")
        )
        if len(candidates) == 1:
            snap = candidates[0]
        else:
            raise FileNotFoundError(
                f"snapshot {snapshot_name!r} not found in "
                f"{_backups_root(data_dir) / 'db'}"
            )
    target = root / "korpha.db"
    if create_safety_copy and target.is_file():
        safety = root / f"korpha.db.before-restore.{int(time.time())}"
        shutil.copy2(target, safety)
        logger.info("safety copy of live DB: %s", safety)
    shutil.copy2(snap, target)
    logger.warning("restored %s → %s", snap.name, target)
    return target


__all__ = [
    "BackupInfo",
    "BackupKind",
    "RetentionPolicy",
    "apply_retention",
    "list_backups",
    "restore_db_snapshot",
    "take_db_snapshot",
    "take_full_backup",
]
