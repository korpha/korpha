"""Readiness checks — is THIS machine ready to receive a bundle?

Run before an operator commits to ``korpha migrate restore``. Catches
the boring stuff:

  - Python interpreter meets the floor declared in pyproject.toml's
    ``requires-python`` (currently 3.11+)
  - Enough free disk space at the data-dir target
  - The data dir isn't already populated (would refuse to clobber)
  - Optional bundle compatibility — if the operator points us at a
    bundle, we compare its source python_version against ours

Returns a list of structured ``Check`` results so the CLI can print
them neatly + return non-zero on the first hard FAIL. INFO-level
checks (e.g., "minor version mismatch") still let the operator
proceed.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from korpha.migrate.manifest import Manifest


class CheckLevel(str, Enum):
    """How critical the result is.

    PASS — green light, nothing to do.
    INFO — worth surfacing but not blocking (e.g. version mismatch
           within the same major series).
    WARN — proceed at your own risk (e.g. low disk space, target
           dir non-empty without force).
    FAIL — restore will not work — fix before retrying.
    """

    PASS = "PASS"
    INFO = "INFO"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class Check:
    """One readiness probe result."""

    name: str
    level: CheckLevel
    message: str


_MIN_PYTHON = (3, 11)
"""Hard floor. Must track ``requires-python`` in ``pyproject.toml`` —
that is the source of truth for what interpreters can install korpha.
A drift-guard test (``test_min_python_matches_pyproject``) asserts the
two stay aligned."""


_MIN_FREE_BYTES_DEFAULT = 200 * 1024 * 1024
"""Default low-water mark for free disk space, 200 MB. Bundle
restores are typically <100 MB; 200 MB headroom keeps the post-
restore data dir + sqlite write-ahead logs comfortable. Caller
can override if they know the bundle size."""


def check_python_version(
    *, required: tuple[int, int] = _MIN_PYTHON,
) -> Check:
    """Current interpreter meets the minimum version."""
    have = (sys.version_info.major, sys.version_info.minor)
    have_str = f"{have[0]}.{have[1]}.{sys.version_info.micro}"
    if have >= required:
        return Check(
            name="python_version",
            level=CheckLevel.PASS,
            message=f"Python {have_str} ≥ {required[0]}.{required[1]}",
        )
    return Check(
        name="python_version",
        level=CheckLevel.FAIL,
        message=(
            f"Python {have_str} is below the required "
            f"{required[0]}.{required[1]}. Install a newer interpreter."
        ),
    )


def check_disk_space(
    data_dir: Path,
    *,
    min_free_bytes: int = _MIN_FREE_BYTES_DEFAULT,
) -> Check:
    """Probe free space on the filesystem holding ``data_dir``.

    Uses ``shutil.disk_usage`` against the data dir's parent (the
    data dir might not exist yet). Returns WARN when below the
    minimum so operators can override.
    """
    probe = data_dir.parent if not data_dir.exists() else data_dir
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return Check(
            name="disk_space",
            level=CheckLevel.WARN,
            message=f"could not probe disk usage at {probe}: {exc}",
        )
    free_mb = usage.free / (1024 * 1024)
    if usage.free >= min_free_bytes:
        return Check(
            name="disk_space",
            level=CheckLevel.PASS,
            message=f"{free_mb:.0f} MB free at {probe}",
        )
    return Check(
        name="disk_space",
        level=CheckLevel.WARN,
        message=(
            f"only {free_mb:.0f} MB free at {probe} — "
            f"restore wants {min_free_bytes // (1024 * 1024)} MB headroom"
        ),
    )


def check_data_dir_empty(data_dir: Path) -> Check:
    """Target data dir must be empty (or absent) for a non-force restore."""
    if not data_dir.exists():
        return Check(
            name="data_dir_empty",
            level=CheckLevel.PASS,
            message=f"{data_dir} does not exist yet — clean target",
        )
    if not any(data_dir.iterdir()):
        return Check(
            name="data_dir_empty",
            level=CheckLevel.PASS,
            message=f"{data_dir} is empty — clean target",
        )
    return Check(
        name="data_dir_empty",
        level=CheckLevel.WARN,
        message=(
            f"{data_dir} already has contents — restore will refuse "
            "unless you pass --force (or back up the existing dir first)"
        ),
    )


def check_bundle_compatibility(manifest: Manifest) -> Check:
    """When the operator pointed us at a bundle, compare versions.

    Same major.minor python = PASS. Off by one minor = INFO. Off by
    more = WARN (restored data might rely on stdlib behaviour that
    changed). Different OS family is informational only.
    """
    source_parts = manifest.source.python_version.split(".")
    try:
        source_major, source_minor = int(source_parts[0]), int(source_parts[1])
    except (ValueError, IndexError):
        return Check(
            name="bundle_python",
            level=CheckLevel.INFO,
            message=(
                f"bundle python version is "
                f"{manifest.source.python_version!r} — couldn't parse"
            ),
        )
    target_major = sys.version_info.major
    target_minor = sys.version_info.minor

    if (source_major, source_minor) == (target_major, target_minor):
        return Check(
            name="bundle_python",
            level=CheckLevel.PASS,
            message=(
                f"bundle python {manifest.source.python_version} matches "
                f"target {target_major}.{target_minor}"
            ),
        )
    if source_major == target_major and abs(source_minor - target_minor) == 1:
        return Check(
            name="bundle_python",
            level=CheckLevel.INFO,
            message=(
                f"bundle python {manifest.source.python_version}, "
                f"target {target_major}.{target_minor} — close enough"
            ),
        )
    return Check(
        name="bundle_python",
        level=CheckLevel.WARN,
        message=(
            f"bundle python {manifest.source.python_version} vs "
            f"target {target_major}.{target_minor} — sqlite + pickled "
            "state may behave differently"
        ),
    )


def run_readiness_checks(
    data_dir: Path,
    *,
    manifest: Manifest | None = None,
) -> list[Check]:
    """Run the standard probe set in deterministic order.

    Pass ``manifest`` when checking against a specific bundle so we
    can compare source/target python versions.
    """
    checks = [
        check_python_version(),
        check_disk_space(data_dir),
        check_data_dir_empty(data_dir),
    ]
    if manifest is not None:
        checks.append(check_bundle_compatibility(manifest))
    return checks


def has_blocking_failures(checks: list[Check]) -> bool:
    """Return True if any check is FAIL — caller should exit non-zero."""
    return any(c.level == CheckLevel.FAIL for c in checks)


__all__ = [
    "Check",
    "CheckLevel",
    "check_bundle_compatibility",
    "check_data_dir_empty",
    "check_disk_space",
    "check_python_version",
    "has_blocking_failures",
    "run_readiness_checks",
]
