"""Build a migration bundle = ``korpha backup`` tarball + manifest.

A migration bundle is a single ``.tar.gz`` that holds:

  - ``korpha/`` — the full data dir (same payload as ``korpha backup``).
  - ``korpha-migration.json`` — the manifest written by
    :mod:`korpha.migrate.manifest`.

The two are tarred together so an operator can scp / rsync a single
file to the target and restore in one step.
"""
from __future__ import annotations

import io
import os
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from korpha.migrate.manifest import (
    MIGRATION_MANIFEST_FILENAME,
    Manifest,
    build_manifest,
)


@dataclass
class BundleResult:
    """What ``create_migration_bundle`` returns to its CLI caller.

    Returned even on success so the CLI can show the bundle size +
    "restore with: ..." hint, AND echo the cred-reauth summary.
    """

    bundle_path: Path
    manifest: Manifest
    bytes_written: int


def create_migration_bundle(
    data_dir: Path,
    output_path: Path,
    *,
    home: Path | None = None,
) -> BundleResult:
    """Write a migration bundle to ``output_path``.

    Steps:
      1. Build a manifest snapshotting source state + cred audit.
      2. Open ``output_path`` as a gzip tarball.
      3. Add ``data_dir`` under ``korpha/``.
      4. Add the manifest at ``korpha-migration.json``.
      5. Re-open the tarball to read its on-disk size, populate
         ``manifest.bundle_size_bytes`` for callers that just want
         the metadata.

    Raises ``FileNotFoundError`` if ``data_dir`` doesn't exist —
    surfaces as a friendly error in the CLI rather than a
    half-written tarball.
    """
    if not data_dir.is_dir():
        raise FileNotFoundError(
            f"data dir not found: {data_dir} — "
            "run `korpha init` first."
        )
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(data_dir, home=home)
    manifest_bytes = manifest.to_json().encode("utf-8")

    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(data_dir, arcname="korpha")
        info = tarfile.TarInfo(name=MIGRATION_MANIFEST_FILENAME)
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(manifest_bytes))

    bytes_written = output_path.stat().st_size
    manifest.bundle_size_bytes = bytes_written

    return BundleResult(
        bundle_path=output_path,
        manifest=manifest,
        bytes_written=bytes_written,
    )


__all__ = [
    "BundleResult",
    "create_migration_bundle",
]
