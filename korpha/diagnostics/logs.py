"""Structured-JSONL log persistence + tail helpers.

We add a rotating file handler at ``~/.korpha/logs/korpha.log``
so ``korpha logs`` has something to read. Stderr stays on too —
foreground operators keep seeing live output.

JSONL records are easier to filter / time-slice than free-form
strings. Each line:

    {"ts": "2026-05-07T12:34:56.789Z", "level": "INFO",
     "logger": "korpha.cofounder.ceo", "msg": "...", "extra": {...}}

Rotating by size (10 MB / 5 backups by default) — small enough that a
laptop dev session doesn't blow the disk, big enough to capture days
of normal operation.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def _default_log_dir() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return (Path(base) / "logs") if base else (Path.home() / ".korpha" / "logs")


DEFAULT_LOG_PATH: Path = _default_log_dir() / "korpha.log"


class _JsonLineFormatter(logging.Formatter):
    """Renders each record as one JSON object on its own line.

    Anything passed via ``logger.info("...", extra={...})`` lands
    in the ``extra`` field — keys that collide with the standard
    record attributes (filename, lineno, etc.) are skipped to avoid
    noise.
    """

    _RESERVED = frozenset({
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload: dict[str, Any] = {
            "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra: dict[str, Any] = {}
        for key, val in record.__dict__.items():
            if key in self._RESERVED:
                continue
            try:
                json.dumps(val, default=str)  # smoke-check serializability
                extra[key] = val
            except (TypeError, ValueError):
                extra[key] = repr(val)
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_INSTALLED: bool = False


def install_file_handler(
    path: Path | None = None,
    *,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
) -> Path:
    """Install a rotating JSONL handler on the root logger.

    Idempotent — multiple calls install once. Returns the resolved
    log path so the caller can echo it (``korpha server`` prints
    "logs going to ..." at startup).
    """
    global _INSTALLED
    target = path if path is not None else DEFAULT_LOG_PATH
    if _INSTALLED:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        target,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(_JsonLineFormatter())
    handler.set_name("korpha.jsonl")

    root = logging.getLogger()
    # Avoid duplicate installs if some other code path already
    # added our named handler.
    for h in root.handlers:
        if h.get_name() == "korpha.jsonl":
            _INSTALLED = True
            return target
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    _INSTALLED = True
    return target


def iter_log_records(
    path: Path | None = None,
    *,
    min_level: str | None = None,
    since: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed records from the log file.

    Honours optional ``min_level`` (e.g. ``"WARNING"`` filters out
    INFO/DEBUG) and ``since`` (timezone-aware datetime — yields only
    records whose ``ts`` is at or after this).

    Returns an empty iterator if the file doesn't exist yet.
    Malformed JSON lines are skipped silently — the file is meant
    to be operator-readable but we don't want one botched line to
    break the tail.
    """
    target = path if path is not None else DEFAULT_LOG_PATH
    if not target.exists():
        return
    level_threshold = _level_to_int(min_level)
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if level_threshold is not None:
                rec_level = _level_to_int(record.get("level"))
                if rec_level is None or rec_level < level_threshold:
                    continue
            if since is not None:
                ts_raw = record.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if ts < since:
                    continue
            yield record


def _level_to_int(name: str | None) -> int | None:
    if not name:
        return None
    return logging.getLevelNamesMapping().get(name.upper())


def tail_log(
    path: Path | None = None,
    *,
    min_level: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    follow: bool = False,
    poll_seconds: float = 0.5,
) -> Iterator[dict[str, Any]]:
    """Like ``iter_log_records`` but with optional ``follow`` mode
    (re-read on new lines). Limit caps the initial backlog count
    when set."""
    target = path if path is not None else DEFAULT_LOG_PATH
    initial = list(iter_log_records(
        target, min_level=min_level, since=since,
    ))
    if limit is not None and len(initial) > limit:
        initial = initial[-limit:]
    yield from initial

    if not follow:
        return
    import time
    pos = target.stat().st_size if target.exists() else 0
    while True:
        time.sleep(poll_seconds)
        if not target.exists():
            continue
        size = target.stat().st_size
        if size < pos:
            # Rotation — the file shrunk because the writer rolled
            # to a new file. Reset to 0 so we read the new content.
            pos = 0
        if size == pos:
            continue
        with target.open("r", encoding="utf-8") as fh:
            fh.seek(pos)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if min_level is not None:
                    rec_level = _level_to_int(rec.get("level"))
                    threshold = _level_to_int(min_level)
                    if rec_level is None or threshold is None or rec_level < threshold:
                        continue
                yield rec
            pos = fh.tell()
