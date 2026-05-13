"""Workspace checkpoint manager — tarball snapshots + restore.

Why: ``code.ship_via_codex`` lets Codex mutate the user's repo
files. One agent slip-up — a bad refactor, an over-eager rewrite,
a deletion of the wrong file — and the founder loses work that
existed seconds ago. Without snapshots, "undo last Codex" is
impossible. With snapshots, it's a single CLI invocation.

Architecture:

  - ``snapshot(workspace_path, label)`` writes a tar.gz at
    ``~/.korpha/checkpoints/<workspace-slug>/<id>.tar.gz`` plus
    a sidecar ``<id>.json`` manifest with timestamp + label +
    file count.
  - ``restore(workspace_path, snapshot_id)`` extracts the tarball
    over the workspace, replacing the current state. Anything
    untracked since the snapshot is overwritten — a separate
    pre-restore snapshot is taken automatically so the user can
    redo if they realize they wanted what they had.
  - ``list_checkpoints(workspace_path)`` returns sorted manifests
    for display.
  - ``prune(workspace_path, keep_last=N)`` removes oldest snapshots
    beyond the cap. Defaults keep 20.

Adapted from Hermes' ``tools/checkpoint_manager.py`` (which uses
shadow-git for content-addressed dedup). Korpha takes the
simpler tar.gz approach because:
  - Most user repos are small (< 100 MB)
  - Content dedup matters at scale (1000s of snapshots / day);
    Mike runs maybe 5-20 Codex commands per day
  - tar.gz is dependency-free; shadow-git needs a git binary
    + indexer
"""
from korpha.checkpoints.manager import (
    Checkpoint,
    CheckpointError,
    DEFAULT_KEEP_LAST,
    list_checkpoints,
    prune,
    restore,
    snapshot,
)

__all__ = [
    "Checkpoint",
    "CheckpointError",
    "DEFAULT_KEEP_LAST",
    "list_checkpoints",
    "prune",
    "restore",
    "snapshot",
]
