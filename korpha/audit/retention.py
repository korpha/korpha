"""Audit log retention — archive old Activity/Cost rows to compressed
JSONL files so the live DB stays lean.

Every LLM call writes one ``Cost`` row. Every mutating skill writes
one ``Activity`` row. Over 180 days a casual user sees ~50k rows;
an active solopreneur with daily Codex sessions can hit 500k. The
DB still works at that scale, but ``korpha insights`` queries
get slow and ``VACUUM`` takes minutes.

Strategy: rows older than ``days_keep`` (default 180) get appended
to per-month JSONL.gz archive files at::

    ~/.korpha/archive/activity-2025-12.jsonl.gz
    ~/.korpha/archive/cost-2025-12.jsonl.gz

Then deleted from the DB. Archive files are append-only — running
the archive twice on the same window is idempotent (we re-write
only the still-in-DB rows; rows already deleted on the first pass
don't reappear).

The archive format is plain JSONL: one JSON object per line, with
``id`` / ``created_at`` / etc. exactly as serialized to the DB.
Future ``korpha audit replay`` (out of scope here) can stream
these back if Mike needs to query historical activity.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import Activity, Cost

logger = logging.getLogger(__name__)


DEFAULT_DAYS_KEEP = 180
"""How long Activity/Cost rows stay in the live DB before archive.
Six months covers Mike's "what did the agent do last quarter?"
queries, and keeps the active DB small enough that VACUUM stays
under a minute on a typical laptop."""


@dataclass(frozen=True)
class ArchiveStats:
    """Summary returned by ``archive_activity`` / ``archive_cost``."""

    rows_archived: int
    bytes_written: int
    months_touched: list[str]
    """ISO ``YYYY-MM`` strings — handy for the CLI to list which
    archive files now exist."""


def _archive_root() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        (Path(base) / "archive")
        if base
        else (Path.home() / ".korpha" / "archive")
    )


def _json_default(obj: object) -> object:
    """Serializer for the few non-JSON-native types in our models."""
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"unsupported type for archive: {type(obj).__name__}")


def _activity_to_dict(row: Activity) -> dict:
    return {
        "id": row.id,
        "business_id": row.business_id,
        "actor_type": row.actor_type.value,
        "actor_id": row.actor_id,
        "event_type": row.event_type,
        "payload": row.payload,
        "created_at": row.created_at,
    }


def _cost_to_dict(row: Cost) -> dict:
    return {
        "id": row.id,
        "business_id": row.business_id,
        "agent_role_id": row.agent_role_id,
        "task_id": row.task_id,
        "thread_id": row.thread_id,
        "provider": row.provider,
        "model": row.model,
        "tier": row.tier.value,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cached_tokens": row.cached_tokens,
        "cost_usd": row.cost_usd,
        "created_at": row.created_at,
    }


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _append_jsonl_gz(path: Path, lines: list[str]) -> int:
    """Append ``lines`` to a gzipped JSONL file. Returns bytes written.

    Atomic per-call: we write to ``<path>.tmp``, fsync, then rename.
    A crash mid-append never corrupts the existing archive — the
    .tmp gets swept by ``korpha disk vacuum``."""
    if not lines:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: bytes = b""
    if path.is_file():
        with path.open("rb") as f:
            existing = f.read()
    payload = "\n".join(lines).encode("utf-8") + b"\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=6) as gz:
        if existing:
            # Decompress + re-compress to keep one continuous gzip
            # stream (concatenated gzip streams are valid but make
            # naive readers stop after the first member).
            try:
                with gzip.open(path, "rb") as old:
                    gz.write(old.read())
            except OSError:
                # Existing file was partial / unreadable — start
                # over rather than block the archive.
                logger.warning(
                    "archive: existing %s unreadable; rewriting", path,
                )
        gz.write(payload)
    os.replace(tmp, path)
    return path.stat().st_size


def archive_activity(
    session: Session,
    *,
    days_keep: int = DEFAULT_DAYS_KEEP,
    archive_dir: Path | None = None,
    delete_after: bool = True,
) -> ArchiveStats:
    """Move ``Activity`` rows older than ``days_keep`` to disk archive."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days_keep)
    target_root = archive_dir or _archive_root()

    rows: list[Activity] = list(session.exec(
        select(Activity).where(Activity.created_at < cutoff)
    ).all())
    if not rows:
        return ArchiveStats(0, 0, [])

    by_month: dict[str, list[str]] = {}
    for r in rows:
        key = _month_key(r.created_at)
        line = json.dumps(
            _activity_to_dict(r), default=_json_default,
            separators=(",", ":"),
        )
        by_month.setdefault(key, []).append(line)

    bytes_written = 0
    for month, lines in by_month.items():
        path = target_root / f"activity-{month}.jsonl.gz"
        bytes_written += _append_jsonl_gz(path, lines)

    if delete_after:
        for r in rows:
            session.delete(r)
        session.commit()

    return ArchiveStats(
        rows_archived=len(rows),
        bytes_written=bytes_written,
        months_touched=sorted(by_month.keys()),
    )


def archive_cost(
    session: Session,
    *,
    days_keep: int = DEFAULT_DAYS_KEEP,
    archive_dir: Path | None = None,
    delete_after: bool = True,
) -> ArchiveStats:
    """Move ``Cost`` rows older than ``days_keep`` to disk archive."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days_keep)
    target_root = archive_dir or _archive_root()

    rows: list[Cost] = list(session.exec(
        select(Cost).where(Cost.created_at < cutoff)
    ).all())
    if not rows:
        return ArchiveStats(0, 0, [])

    by_month: dict[str, list[str]] = {}
    for r in rows:
        key = _month_key(r.created_at)
        line = json.dumps(
            _cost_to_dict(r), default=_json_default,
            separators=(",", ":"),
        )
        by_month.setdefault(key, []).append(line)

    bytes_written = 0
    for month, lines in by_month.items():
        path = target_root / f"cost-{month}.jsonl.gz"
        bytes_written += _append_jsonl_gz(path, lines)

    if delete_after:
        for r in rows:
            session.delete(r)
        session.commit()

    return ArchiveStats(
        rows_archived=len(rows),
        bytes_written=bytes_written,
        months_touched=sorted(by_month.keys()),
    )


def archive_all(
    session: Session,
    *,
    days_keep: int = DEFAULT_DAYS_KEEP,
    archive_dir: Path | None = None,
) -> dict[str, ArchiveStats]:
    """Archive both tables in one call. Returns a per-table dict."""
    return {
        "activity": archive_activity(
            session, days_keep=days_keep, archive_dir=archive_dir,
        ),
        "cost": archive_cost(
            session, days_keep=days_keep, archive_dir=archive_dir,
        ),
    }


def archive_size_breakdown(
    archive_dir: Path | None = None,
) -> dict:
    """List archive files + their on-disk sizes. Used by
    ``korpha disk`` to surface archive footprint alongside the
    live DB."""
    target_root = archive_dir or _archive_root()
    if not target_root.is_dir():
        return {"total_bytes": 0, "files": []}
    files: list[dict] = []
    total = 0
    for p in sorted(target_root.glob("*.jsonl.gz")):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        total += size
        files.append({"name": p.name, "bytes": size})
    return {"total_bytes": total, "files": files}


__all__ = [
    "ArchiveStats",
    "DEFAULT_DAYS_KEEP",
    "archive_activity",
    "archive_all",
    "archive_cost",
    "archive_size_breakdown",
]
