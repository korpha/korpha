"""Tarball-based workspace snapshots with sidecar manifests."""
from __future__ import annotations

import json
import logging
import os
import re
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_KEEP_LAST = 20
"""How many snapshots to retain per workspace before pruning. Mike
runs maybe 20 Codex commands a day — keeping 20 = roughly one
day's worth, plenty for "undo what just broke" without growing
unbounded on disk."""

_MAX_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
"""Refuse to snapshot huge workspaces. node_modules / .venv / build
artifacts blow this fast — the founder needs to add an exclusion
file (or shrink their tree) before checkpoints work."""

_DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
    ".next",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)
"""Directories we never include in snapshots. These are either
regenerable (build artifacts), VCS-tracked elsewhere (.git), or
huge (node_modules). The user can add a .korpha-checkpoint-ignore
to extend this list per workspace."""


class CheckpointError(RuntimeError):
    """Snapshot or restore failed. Surface to the caller."""


@dataclass(frozen=True)
class Checkpoint:
    """One snapshot's manifest. Loaded from the sidecar JSON."""

    id: str
    workspace_path: str
    label: str
    created_at: str
    """ISO-8601 UTC. String rather than datetime so the on-disk
    format is human-readable + survives timezone serialization
    edge cases."""
    file_count: int
    size_bytes: int

    @property
    def created_dt(self) -> datetime:
        """Parsed ``created_at`` for display sort. Falls back to
        epoch for malformed input rather than raising."""
        try:
            return datetime.fromisoformat(
                self.created_at.replace("Z", "+00:00"),
            )
        except (ValueError, AttributeError):
            return datetime.fromtimestamp(0, tz=timezone.utc)


# ---- paths ----------------------------------------------------------


def _checkpoints_root() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        (Path(base) / "checkpoints")
        if base
        else (Path.home() / ".korpha" / "checkpoints")
    )


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _workspace_slug(workspace_path: Path) -> str:
    """Filesystem-safe shortened name for the workspace dir. Used
    as the per-workspace checkpoint folder. Append a hash of the
    full path to disambiguate two workspaces with the same basename
    (``~/code/cofounder`` vs ``~/projects/cofounder``)."""
    full = str(workspace_path.expanduser().resolve())
    base = workspace_path.expanduser().resolve().name or "workspace"
    safe = _SLUG_RE.sub("-", base).strip("-") or "workspace"
    suffix = abs(hash(full)) % (10**6)
    return f"{safe[:60]}-{suffix:06d}"


def _per_workspace_dir(workspace_path: Path) -> Path:
    return _checkpoints_root() / _workspace_slug(workspace_path)


# ---- exclusion ------------------------------------------------------


def _resolve_excludes(workspace_path: Path) -> set[str]:
    """Combine defaults with anything in
    ``.korpha-checkpoint-ignore``. One pattern per line — exact
    directory/file name match (no globs; keeps the format dead
    simple)."""
    excludes = set(_DEFAULT_EXCLUDES)
    ignore_file = workspace_path / ".korpha-checkpoint-ignore"
    if ignore_file.is_file():
        try:
            for line in ignore_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    excludes.add(line)
        except OSError as exc:
            logger.debug(
                "checkpoint: ignore-file read failed (%s); using defaults",
                exc,
            )
    return excludes


def _walk_files(
    workspace: Path, excludes: set[str],
) -> tuple[list[Path], int]:
    """Enumerate files to include + total size. Returns early when
    size > _MAX_SIZE_BYTES so a 50 GB ``node_modules`` accidentally
    untracked doesn't make us tar-zip our way to OOM."""
    files: list[Path] = []
    total = 0
    for root, dirs, names in os.walk(workspace):
        # Mutate dirs in-place to skip excluded subtrees
        dirs[:] = [d for d in dirs if d not in excludes]
        for name in names:
            if name in excludes:
                continue
            fp = Path(root) / name
            try:
                stat = fp.stat()
            except OSError:
                continue
            files.append(fp)
            total += stat.st_size
            if total > _MAX_SIZE_BYTES:
                raise CheckpointError(
                    f"workspace too large to snapshot "
                    f"(>{_MAX_SIZE_BYTES // (1024 * 1024)} MB). Add "
                    "exclusions to .korpha-checkpoint-ignore."
                )
    return files, total


# ---- snapshot -------------------------------------------------------


def snapshot(
    workspace_path: Path | str,
    *,
    label: str = "",
    base_dir: Path | None = None,
) -> Checkpoint:
    """Snapshot the workspace.

    Defaults to v2 (content-addressed blobs, gzipped, dedup across
    snapshots). Set ``KORPHA_CHECKPOINT_FORMAT=v1`` to fall back
    to legacy per-snapshot tar.gz — kept for users who haven't
    migrated yet via ``korpha checkpoints vacuum``.

    ``label`` is a short human-readable note ("before codex refactor",
    "pre-restore auto") — surfaced in ``korpha checkpoints list``.
    """
    fmt = os.environ.get("KORPHA_CHECKPOINT_FORMAT", "v2").strip().lower()
    if fmt != "v1":
        # Lazy import to avoid a v2 ↔ manager circular import on load.
        from korpha.checkpoints.v2 import snapshot_v2
        return snapshot_v2(
            workspace_path, label=label, base_dir=base_dir,
        )
    return _snapshot_v1(
        workspace_path, label=label, base_dir=base_dir,
    )


def _snapshot_v1(
    workspace_path: Path | str,
    *,
    label: str = "",
    base_dir: Path | None = None,
) -> Checkpoint:
    """v1 tar.gz snapshot — kept for compatibility + escape hatch."""
    workspace = Path(workspace_path).expanduser().resolve()
    if not workspace.is_dir():
        raise CheckpointError(
            f"snapshot: workspace {workspace} is not a directory"
        )

    excludes = _resolve_excludes(workspace)
    files, total = _walk_files(workspace, excludes)

    cid = uuid.uuid4().hex[:12]
    target_dir = (
        base_dir if base_dir is not None
        else _per_workspace_dir(workspace)
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    archive_path = target_dir / f"{cid}.tar.gz"
    manifest_path = target_dir / f"{cid}.json"

    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            for fp in files:
                arcname = str(fp.relative_to(workspace))
                tar.add(fp, arcname=arcname, recursive=False)
    except (OSError, tarfile.TarError) as exc:
        # Best-effort cleanup of a partial tarball before re-raise.
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CheckpointError(f"snapshot: tar write failed: {exc}") from exc

    created_at = (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    checkpoint = Checkpoint(
        id=cid,
        workspace_path=str(workspace),
        label=label.strip(),
        created_at=created_at,
        file_count=len(files),
        size_bytes=archive_path.stat().st_size,
    )
    manifest_path.write_text(
        json.dumps(_to_manifest(checkpoint), indent=2),
        encoding="utf-8",
    )
    logger.info(
        "checkpoint: snapshot %s for %s (%d files, %d bytes)",
        cid, workspace, len(files), checkpoint.size_bytes,
    )
    return checkpoint


def _to_manifest(c: Checkpoint) -> dict:
    return {
        "id": c.id,
        "workspace_path": c.workspace_path,
        "label": c.label,
        "created_at": c.created_at,
        "file_count": c.file_count,
        "size_bytes": c.size_bytes,
    }


def _from_manifest(data: dict) -> Checkpoint | None:
    try:
        return Checkpoint(
            id=str(data["id"]),
            workspace_path=str(data.get("workspace_path") or ""),
            label=str(data.get("label") or ""),
            created_at=str(data.get("created_at") or ""),
            file_count=int(data.get("file_count") or 0),
            size_bytes=int(data.get("size_bytes") or 0),
        )
    except (KeyError, ValueError, TypeError):
        return None


# ---- list / restore / prune -----------------------------------------


def list_checkpoints(
    workspace_path: Path | str,
    *,
    base_dir: Path | None = None,
) -> list[Checkpoint]:
    """Sorted newest-first. Empty list when no checkpoints exist.

    Returns both v1 (tar.gz + sidecar) and v2 (manifest-only +
    content-addressed blobs) snapshots — callers don't need to care
    which format any given snapshot uses."""
    workspace = Path(workspace_path).expanduser().resolve()
    target_dir = (
        base_dir if base_dir is not None
        else _per_workspace_dir(workspace)
    )
    if not target_dir.is_dir():
        return []
    out: list[Checkpoint] = []
    seen_ids: set[str] = set()
    # v2 manifests are named ``<id>.v2.json`` — process them first so
    # if a workspace has both formats for the same id (transitional
    # state during vacuum), the v2 entry wins.
    from korpha.checkpoints.v2 import list_v2_snapshots
    for cp in list_v2_snapshots(target_dir):
        if cp.id in seen_ids:
            continue
        out.append(cp)
        seen_ids.add(cp.id)
    # v1 sidecars are ``<id>.json`` (without .v2). Skip the v2
    # manifests by checking the suffix explicitly.
    for path in target_dir.glob("*.json"):
        if path.name.endswith(".v2.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cp = _from_manifest(data)
        if (
            cp is not None
            and cp.id not in seen_ids
            and (target_dir / f"{cp.id}.tar.gz").exists()
        ):
            out.append(cp)
            seen_ids.add(cp.id)
    out.sort(key=lambda c: c.created_dt, reverse=True)
    return out


def restore(
    workspace_path: Path | str,
    snapshot_id: str,
    *,
    base_dir: Path | None = None,
    auto_pre_snapshot: bool = True,
) -> Checkpoint:
    """Extract ``snapshot_id`` over the workspace.

    Auto-snapshots the *current* state first (label
    ``"pre-restore-<short-id>"``) so a "wait, I wanted that" is
    one more restore call away. Set ``auto_pre_snapshot=False`` to
    skip — useful for tests + nested restores.

    Returns the pre-restore snapshot (or the original target if
    auto_pre_snapshot=False) so the caller can show the user
    "to undo this restore: restore <id>"."""
    workspace = Path(workspace_path).expanduser().resolve()
    target_dir = (
        base_dir if base_dir is not None
        else _per_workspace_dir(workspace)
    )
    # v2 path: manifest-driven content-addressed restore.
    from korpha.checkpoints.v2 import is_v2_snapshot, restore_v2
    if is_v2_snapshot(target_dir, snapshot_id):
        pre_cp_v2: Checkpoint | None = None
        if auto_pre_snapshot:
            try:
                pre_cp_v2 = snapshot(
                    workspace,
                    label=f"pre-restore-{snapshot_id[:6]}",
                    base_dir=base_dir,
                )
            except CheckpointError as exc:
                logger.warning(
                    "checkpoint: pre-restore snapshot failed (%s)",
                    exc,
                )
        restore_v2(workspace, snapshot_id, base_dir=base_dir)
        return pre_cp_v2 if pre_cp_v2 is not None else Checkpoint(
            id=snapshot_id,
            workspace_path=str(workspace),
            label="(restored)",
            created_at="",
            file_count=0,
            size_bytes=0,
        )

    archive = target_dir / f"{snapshot_id}.tar.gz"
    if not archive.is_file():
        raise CheckpointError(
            f"restore: snapshot {snapshot_id!r} not found at {archive}"
        )

    pre_cp: Checkpoint | None = None
    if auto_pre_snapshot:
        try:
            pre_cp = snapshot(
                workspace,
                label=f"pre-restore-{snapshot_id[:6]}",
                base_dir=base_dir,
            )
        except CheckpointError as exc:
            # Don't block the restore on failure to pre-snapshot;
            # log loudly so the founder knows there's no undo path.
            logger.warning(
                "checkpoint: pre-restore snapshot failed (%s); "
                "restoring without rollback safety net", exc,
            )

    try:
        with tarfile.open(archive, "r:gz") as tar:
            # Defensive extraction — refuse paths that escape the
            # workspace (zip-slip / tar-slip mitigation). Should
            # never happen with our own tarballs but the guard is
            # cheap and a future bug-induced bad write here would be
            # nightmarish.
            for member in tar.getmembers():
                _ensure_safe_member(member, workspace)
            # ``filter='data'`` is the secure-by-default mode in
            # Python 3.12+ (will be the default in 3.14). Setting
            # explicitly silences the deprecation warning + keeps
            # the strict path semantics that pair with our own
            # _ensure_safe_member check.
            tar.extractall(workspace, filter="data")
    except (OSError, tarfile.TarError, CheckpointError) as exc:
        raise CheckpointError(f"restore: extract failed: {exc}") from exc

    logger.info(
        "checkpoint: restored %s into %s (pre-snapshot %s)",
        snapshot_id, workspace,
        pre_cp.id if pre_cp else "skipped",
    )
    return pre_cp if pre_cp is not None else Checkpoint(
        id=snapshot_id,
        workspace_path=str(workspace),
        label="(restored)",
        created_at="",
        file_count=0,
        size_bytes=archive.stat().st_size,
    )


def _ensure_safe_member(member: tarfile.TarInfo, workspace: Path) -> None:
    """Reject paths that escape the workspace (../, absolute paths,
    symlinks pointing outside the tree)."""
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        raise CheckpointError(
            f"restore: refusing unsafe member path {name!r}"
        )
    if member.issym() or member.islnk():
        target = (
            (workspace / name).parent / member.linkname
        ).resolve()
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise CheckpointError(
                f"restore: refusing symlink {name!r} pointing outside "
                f"workspace ({member.linkname!r})"
            ) from exc


def prune(
    workspace_path: Path | str,
    *,
    keep_last: int = DEFAULT_KEEP_LAST,
    base_dir: Path | None = None,
) -> int:
    """Remove oldest checkpoints beyond ``keep_last``. Returns the
    count actually removed."""
    cps = list_checkpoints(workspace_path, base_dir=base_dir)
    if len(cps) <= keep_last:
        return 0
    to_remove = cps[keep_last:]
    target_dir = (
        base_dir if base_dir is not None
        else _per_workspace_dir(Path(workspace_path).expanduser().resolve())
    )
    removed = 0
    for cp in to_remove:
        for ext in (".tar.gz", ".json"):
            try:
                (target_dir / f"{cp.id}{ext}").unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(
                    "checkpoint: prune unlink failed for %s%s: %s",
                    cp.id, ext, exc,
                )
        removed += 1
    return removed
