"""Restore a migration bundle on the target machine.

Pulls the data dir out of the bundle (same as ``korpha restore``)
THEN walks the operator through the re-auth wizard for any
machine-tied credentials flagged in the manifest.

Plain ``korpha backup`` tarballs (no manifest) are still accepted —
they restore data and skip the wizard.

Restore design:

  - **Single-tarball input**. Operator provides one ``.tar.gz``; we
    figure out whether it has a manifest or not.
  - **Refuse to clobber an existing data dir** unless ``--force`` is
    passed (mirrors ``korpha restore``).
  - **Wizard is interactive but cancellable**. Each cred shows what
    needs to happen + the shell command. Operator hits ENTER after
    completing each step, or types `skip` to defer.
"""
from __future__ import annotations

import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from korpha.migrate.manifest import (
    MIGRATION_MANIFEST_FILENAME,
    Manifest,
    load_manifest,
)


@dataclass
class RestoreResult:
    """Returned to the CLI so it can show a final summary.

    ``manifest`` is None when the input was a plain ``korpha backup``
    tarball (no manifest embedded). ``reauth_skipped`` tracks how
    many wizard steps the operator deferred — surfaced as a tip
    ("re-run `aigenteur migrate reauth` later").
    """

    data_dir: Path
    manifest: Manifest | None
    reauth_completed: int = 0
    reauth_skipped: int = 0


def _is_safe_member_name(name: str) -> bool:
    """Reject tarball entries that try to escape the extraction root.

    ``tarfile`` raises a deprecation warning about the default
    extraction filter from Python 3.12+ for exactly this reason —
    we apply the check explicitly so we keep the behaviour stable
    across Python versions.
    """
    if name.startswith("/"):
        return False
    if ".." in Path(name).parts:
        return False
    return True


def _extract_data_dir(bundle: Path, dest: Path) -> None:
    """Extract the ``korpha/`` payload from the bundle into ``dest``.

    Uses a temp dir + atomic rename so partial extractions don't
    leave the operator with a half-restored data dir.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="korpha-restore-",
        dir=str(dest.parent),
    ) as staging:
        staging_path = Path(staging)
        with tarfile.open(bundle, "r:*") as tar:
            members = [
                m for m in tar.getmembers()
                if _is_safe_member_name(m.name)
                and (m.name == "korpha" or m.name.startswith("korpha/"))
            ]
            tar.extractall(staging_path, members=members, filter="data")  # noqa: S202
        extracted = staging_path / "korpha"
        if not extracted.is_dir():
            raise RuntimeError(
                f"bundle does not contain a `korpha/` directory: {bundle}"
            )
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(extracted), str(dest))


def restore_bundle(
    bundle: Path,
    data_dir: Path,
    *,
    force: bool = False,
) -> RestoreResult:
    """Restore ``bundle`` into ``data_dir``, returning a manifest if
    one was found inside.

    Raises:
        FileNotFoundError: ``bundle`` doesn't exist.
        FileExistsError: ``data_dir`` already has contents and
            ``force=False``.
    """
    bundle = bundle.expanduser().resolve()
    if not bundle.is_file():
        raise FileNotFoundError(f"bundle not found: {bundle}")

    data_dir = data_dir.expanduser().resolve()
    if data_dir.exists() and any(data_dir.iterdir()) and not force:
        raise FileExistsError(
            f"{data_dir} is not empty. Pass force=True (or --force) "
            "to overwrite."
        )

    manifest = load_manifest(bundle)
    _extract_data_dir(bundle, data_dir)
    return RestoreResult(data_dir=data_dir, manifest=manifest)


# ---------------------------------------------------------------------------
# Wizard helpers — UI lives in cli.py, this just yields prompts.
# ---------------------------------------------------------------------------


@dataclass
class ReauthStep:
    """One entry in the wizard sequence.

    The CLI iterates these in order, asks the operator to run
    ``command`` (or marks done if they already did it), then moves on.
    """

    name: str
    command: str
    rationale: str


def reauth_steps_from_manifest(manifest: Manifest) -> list[ReauthStep]:
    """Convert the manifest's machine-tied cred list into wizard
    steps — only includes creds that were present on source.

    Returns an empty list when no creds need re-auth; the wizard
    becomes a no-op and the operator sees "✓ no re-auth needed".
    """
    return [
        ReauthStep(
            name=c.name,
            command=c.reauth_command,
            rationale=c.rationale,
        )
        for c in manifest.credentials_machine_tied
        if c.is_present
    ]


def format_source_banner(manifest: Manifest) -> str:
    """One-line "from X → to Y" diff for the restore command output.

    Used at the top of the wizard so the operator can confirm
    they're restoring the right bundle.
    """
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(manifest.created_at))
    return (
        f"Bundle from {manifest.source.hostname} "
        f"({manifest.source.os}, py {manifest.source.python_version}) "
        f"created {when}"
    )


__all__ = [
    "ReauthStep",
    "RestoreResult",
    "format_source_banner",
    "reauth_steps_from_manifest",
    "restore_bundle",
]
