"""Migration manifest — source-machine metadata bundled with a backup.

A plain ``korpha backup`` tarball captures the data dir, but says
nothing about *which machine produced it* or *what re-auth steps the
target operator needs to take*. The manifest closes that gap.

It lives at ``korpha-migration.json`` inside the bundle and gets read
by:

  - ``korpha migrate restore`` — drives the re-auth wizard, prints the
    source → target diff banner.
  - ``korpha migrate check`` — pre-flight readiness, compares the
    source's korpha + python version against the target's.
  - ``korpha migrate to <user@host>`` — packages the manifest with
    the tarball, ships both via SSH.

By design the manifest is human-readable JSON. If the migration tools
break, the operator can open it in an editor and pick up manually.
"""
from __future__ import annotations

import json
import platform
import socket
import sys
import tarfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from korpha.migrate.cred_audit import MachineTiedCred, scan_machine_tied


MIGRATION_MANIFEST_FILENAME = "korpha-migration.json"
"""Stored at the root of the migration bundle tarball alongside the
``korpha/`` data dir. Stable filename — restore + check tools key
off it."""


_MANIFEST_VERSION = 1
"""Manifest schema version. Bump when adding required fields so old
bundles can still be loaded with a graceful fallback path."""


# ---------------------------------------------------------------------------
# Manifest data
# ---------------------------------------------------------------------------


@dataclass
class SourceInfo:
    """Identifies the machine that produced the bundle.

    Used to display a "from X → to Y" banner during restore so the
    operator can confirm they're restoring the bundle they meant to.
    """

    hostname: str
    os: str
    """``"Linux 6.17.0"``-style platform string. Diagnostic only —
    restore doesn't refuse cross-OS migrations."""

    python_version: str
    """Major.Minor.Patch — the readiness check compares to target."""

    data_dir: str
    """Absolute path to the source's ``KORPHA_DATA_DIR``. Stored as
    a string for JSON round-tripping; readers reconstitute as Path."""


@dataclass
class PendingState:
    """Snapshot of in-flight work at bundle time.

    Mostly informational — the data dir already contains the source
    of truth (sqlite DB has cron job rows, background tasks, active
    business id). Surfacing them up here lets the restore wizard
    say "this bundle had N cron jobs + M background tasks that will
    resume on next start" without parsing the DB twice.
    """

    cron_jobs: int = 0
    background_tasks: int = 0
    active_business_id: str | None = None


@dataclass
class Manifest:
    """The complete migration manifest persisted to JSON.

    ``credentials_machine_tied`` is the catalogue from ``cred_audit``
    with ``is_present`` flipped on for ones found on the source.
    The restore wizard walks just the present ones, skipping any the
    operator never set up.
    """

    manifest_version: int
    """Schema version — see ``_MANIFEST_VERSION``."""

    korpha_version: str
    created_at: float
    """Unix epoch seconds. Wizard formats this for display."""

    source: SourceInfo
    pending: PendingState
    credentials_machine_tied: list[MachineTiedCred] = field(default_factory=list)
    bundle_size_bytes: int | None = None
    """Set after the tarball is finalized — describes the tarball's
    size including the manifest. ``None`` when the manifest is
    written stand-alone (e.g., dry-run, pre-bundle)."""

    # ---- JSON round-trip ------------------------------------------------

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Manifest":
        data = json.loads(raw)
        return cls(
            manifest_version=int(data["manifest_version"]),
            korpha_version=str(data["korpha_version"]),
            created_at=float(data["created_at"]),
            source=SourceInfo(**data["source"]),
            pending=PendingState(**data["pending"]),
            credentials_machine_tied=[
                MachineTiedCred(**c) for c in data.get("credentials_machine_tied", [])
            ],
            bundle_size_bytes=data.get("bundle_size_bytes"),
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _korpha_version() -> str:
    """Return the installed korpha version string, with a graceful
    fallback if importlib.metadata can't find the package (dev edits)."""
    try:
        from importlib.metadata import version
        return version("korpha")
    except Exception:  # noqa: BLE001
        return "0.0.0+dev"


def _snapshot_pending(data_dir: Path) -> PendingState:
    """Best-effort count of in-flight cron + background jobs.

    Uses the sqlite DB directly so we don't have to spin up the full
    SQLAlchemy stack just to take a snapshot. Counts may be slightly
    stale (we read the DB while it could be in use) but that's fine —
    the restore wizard treats these as informational.

    Returns a zero-filled PendingState when the DB is missing or
    unreadable rather than crashing the bundle.
    """
    state = PendingState()
    db_path = data_dir / "korpha.db"
    if not db_path.is_file():
        return state
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = con.cursor()
            for tbl, attr in (
                ("cron_jobs", "cron_jobs"),
                ("background_tasks", "background_tasks"),
            ):
                try:
                    row = cur.execute(
                        f"SELECT COUNT(1) FROM {tbl}"  # noqa: S608
                    ).fetchone()
                    if row:
                        setattr(state, attr, int(row[0]))
                except sqlite3.OperationalError:
                    continue
            try:
                row = cur.execute(
                    "SELECT id FROM businesses "
                    "WHERE is_active = 1 LIMIT 1"
                ).fetchone()
                if row:
                    state.active_business_id = str(row[0])
            except sqlite3.OperationalError:
                pass
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return state
    return state


def build_manifest(
    data_dir: Path,
    *,
    home: Path | None = None,
) -> Manifest:
    """Build a manifest describing the source machine + the contents
    of ``data_dir``.

    Pure read-side: does not touch the data dir beyond reading the
    sqlite DB in shared mode. Safe to call while korpha is running.
    """
    return Manifest(
        manifest_version=_MANIFEST_VERSION,
        korpha_version=_korpha_version(),
        created_at=time.time(),
        source=SourceInfo(
            hostname=socket.gethostname(),
            os=f"{platform.system()} {platform.release()}",
            python_version=(
                f"{sys.version_info.major}."
                f"{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            ),
            data_dir=str(data_dir),
        ),
        pending=_snapshot_pending(data_dir),
        credentials_machine_tied=scan_machine_tied(home=home),
    )


def load_manifest(bundle_path: Path) -> Manifest | None:
    """Read the manifest out of a migration bundle tarball.

    Returns ``None`` if the tarball is a plain ``korpha backup``
    (no manifest inside) so callers can degrade gracefully — a
    plain backup is still restorable, just without the re-auth
    wizard's source-aware prompts.
    """
    try:
        with tarfile.open(bundle_path, "r:*") as tar:
            try:
                member = tar.getmember(MIGRATION_MANIFEST_FILENAME)
            except KeyError:
                return None
            fobj = tar.extractfile(member)
            if fobj is None:
                return None
            raw = fobj.read().decode("utf-8")
            return Manifest.from_json(raw)
    except (tarfile.TarError, OSError):
        return None


__all__ = [
    "MIGRATION_MANIFEST_FILENAME",
    "Manifest",
    "PendingState",
    "SourceInfo",
    "build_manifest",
    "load_manifest",
]
