"""Backup + restore — Mike never loses his business.

Two layers:

1. **Local rotating snapshots** (this module). On by default, zero
   config. Hourly atomic sqlite3 backup + daily full tar.gz of
   the data dir. GFS retention keeps disk usage bounded. Covers
   accidental delete, bad migration, app-level data corruption,
   bad agent action. Does NOT cover total disk loss.

2. **Off-disk push** (``korpha.backup.litestream`` /
   ``korpha.backup.rclone``). Opt-in via dashboard. Pushes the
   snapshots + WAL to S3/R2/B2 or Dropbox/GDrive. THIS is what
   saves Mike when his laptop dies.

Restore is one CLI command or one dashboard click.
"""
from __future__ import annotations

from korpha.backup.snapshot import (
    BackupInfo,
    BackupKind,
    apply_retention,
    list_backups,
    restore_db_snapshot,
    take_db_snapshot,
    take_full_backup,
)

__all__ = [
    "BackupInfo",
    "BackupKind",
    "apply_retention",
    "list_backups",
    "restore_db_snapshot",
    "take_db_snapshot",
    "take_full_backup",
]
