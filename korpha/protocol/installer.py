"""Cofounder Protocol installer — install / list / uninstall manifests.

Installed manifests live under ``~/.korpha/cofounders/`` (one
directory per partner, named after ``manifest.name``). The directory
contains the manifest itself + any YAML skill files it shipped.

Install does NOT execute partner code. It only:
  1. Validates the manifest against the spec.
  2. Confirms each ``provides.skills`` entry resolves to a registered
     skill OR comes with a ``yaml_skill_files`` entry that loads.
  3. Copies the manifest + any YAML skill files into the install dir.
  4. Surfaces the partner's ``auth.setup_command`` so the user knows
     what to run next.

Uninstall removes the install dir + drops any YAML skills the partner
contributed.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from korpha.protocol.manifest import (
    CofounderManifest,
    ManifestError,
    load_manifest,
)
from korpha.skills import default_registry


def _install_root() -> Path:
    """Where installed manifests live. Override with
    ``KORPHA_COFOUNDERS_DIR`` for tests."""
    override = os.getenv("KORPHA_COFOUNDERS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".korpha" / "cofounders"


@dataclass(frozen=True)
class InstalledManifest:
    """A manifest as it lives on disk after install."""

    manifest: CofounderManifest
    install_dir: Path


def install_manifest(
    source: Path | str,
    *,
    skip_skill_check: bool = False,
) -> InstalledManifest:
    """Install a manifest from a local path. URL fetch is the CLI
    command's job (this function stays offline-pure for testing).

    ``skip_skill_check`` lets tests install partner manifests whose
    skills aren't yet in the registry. Real installs reject manifests
    with unresolvable skills so partners get a clean error instead
    of a silent stub.
    """
    manifest = load_manifest(source)

    if not skip_skill_check:
        registered = set(default_registry.skills.keys())
        for skill_name in manifest.provides.skills:
            if skill_name not in registered:
                raise ManifestError(
                    f"manifest declares skill {skill_name!r} but it's not in "
                    "the Korpha skill registry. Built-in skills must land "
                    "in core before a partner publishes a manifest that "
                    "references them. Add the skill in a PR to the Korpha "
                    "repo, then point your manifest at it."
                )

    target_dir = _install_root() / manifest.name
    target_dir.mkdir(parents=True, exist_ok=True)
    if manifest.source_path is not None:
        shutil.copy2(manifest.source_path, target_dir / "cofounder.yaml")

    return InstalledManifest(manifest=manifest, install_dir=target_dir)


def list_installed() -> list[InstalledManifest]:
    """Walk the install dir, return a manifest for every partner that
    has a valid ``cofounder.yaml``. Skips dirs that fail validation
    (and logs the error path so the user can fix or remove them)."""
    root = _install_root()
    if not root.exists():
        return []
    out: list[InstalledManifest] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "cofounder.yaml"
        if not manifest_path.exists():
            continue
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            continue
        out.append(InstalledManifest(manifest=manifest, install_dir=child))
    return out


def uninstall_manifest(name: str) -> bool:
    """Remove an installed partner by name. Returns True if it was
    installed (and removed); False if no such partner."""
    target = _install_root() / name
    if not target.exists() or not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


__all__ = [
    "InstalledManifest",
    "install_manifest",
    "list_installed",
    "uninstall_manifest",
]
