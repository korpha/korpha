"""Local install + publish — bridge between Mike's filesystem and the hub.

Two operations the hub didn't have:

  * ``LocalSource`` — install from a directory on disk OR a
    ``.tar.gz`` bundle Mike downloaded out-of-band (from a
    Discord post, a friend's email, a GitHub release page).
    Same scan-then-install flow as ``KorphaHubSource`` /
    ``GitHubSource``; the only difference is the fetch step
    is local I/O.

  * ``pack_skill()`` — turn an installed-or-authored skill on
    Mike's machine into a sharable tarball. He runs
    ``korpha skill publish my.skill --output ./my-skill.tar.gz``
    and gets a file he can post anywhere — Discord, GitHub,
    his own static site. The next person installs it via
    ``korpha skill install ./my-skill.tar.gz``.

Together these close the BRIEF "moat" loop: community members
ship skills today (install side already worked from
GitHub URLs); now they can publish without spinning up a
GitHub repo.
"""
from __future__ import annotations

import logging
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from korpha.skills_hub.client import (
    SkillBundle,
    SkillMeta,
    SkillSource,
    quarantine_dir,
)

logger = logging.getLogger(__name__)


# Skills get published as tarballs containing a top-level
# directory matching the skill's name. We use this header on
# the manifest so installs can quickly verify the bundle's
# shape before unpacking and scanning.
_PUBLISH_MARKER = ".korpha-skill"


@dataclass
class LocalSource(SkillSource):
    """Install from a path on disk — directory OR tarball.

    Identifier is the absolute path. Search returns either
    nothing (when the path is one specific skill) or a single
    entry describing what's there.
    """

    name: str = "local"

    def search(
        self, query: str = "", *, limit: int = 50,
    ) -> list[SkillMeta]:
        # Local source doesn't browse — it just fetches a path
        # the caller already knows. Return empty so search-style
        # callers don't crash; ``fetch(path)`` is the real entry
        # point.
        return []

    def fetch(self, identifier: str) -> SkillBundle:
        """Identifier = path to a skill directory or .tar.gz bundle.
        Copies / extracts into the quarantine dir and returns
        the bundle so install_skill() can scan + register it."""
        src = Path(identifier).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(
                f"local skill source not found: {src}",
            )
        target_root = quarantine_dir()
        target_root.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            # Directory: copy into quarantine
            skill_name = src.name
            dest = target_root / skill_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            return SkillBundle(
                name=skill_name,
                source="local",
                identifier=str(src),
                quarantine_path=dest,
                metadata={"kind": "directory"},
            )

        # Tarball path
        if src.suffix not in {".gz", ".tgz"} and not src.name.endswith(".tar.gz"):
            raise ValueError(
                f"local skill source must be a directory or "
                f".tar.gz bundle, got {src.name!r}",
            )

        with tarfile.open(src, "r:gz") as tar:
            members = tar.getmembers()
            top_dirs = {
                m.name.split("/", 1)[0]
                for m in members
                if m.name and not m.name.startswith("/")
            }
            if not top_dirs:
                raise ValueError(
                    f"local skill bundle has no entries: {src}",
                )
            if len(top_dirs) > 1:
                raise ValueError(
                    f"local skill bundle must have exactly one "
                    f"top-level directory; found {sorted(top_dirs)}",
                )
            skill_name = next(iter(top_dirs))

            for m in members:
                _ensure_safe_member(m, target_root)

            dest = target_root / skill_name
            if dest.exists():
                shutil.rmtree(dest)
            tar.extractall(target_root, filter="data")

        return SkillBundle(
            name=skill_name,
            source="local",
            identifier=str(src),
            quarantine_path=dest,
            metadata={"kind": "tarball"},
        )


def _ensure_safe_member(
    member: tarfile.TarInfo, target_root: Path,
) -> None:
    """Tar-slip guard. Same posture as our checkpoint v2 +
    LocalFileDeployer write paths — refuse anything that resolves
    outside ``target_root``."""
    if member.name.startswith("/") or ".." in member.name.split("/"):
        raise ValueError(
            f"unsafe member path in skill bundle: {member.name!r}",
        )
    candidate = (target_root / member.name).resolve()
    try:
        candidate.relative_to(target_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"skill bundle member escapes target dir: "
            f"{member.name!r}",
        ) from exc


# ---------------------------------------------------------------------------
# Publish — turn a local skill dir into a sharable tarball
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    """Outcome of pack_skill(). ``output_path`` is the tarball
    Mike posts; ``size_bytes`` shows in the CLI confirmation."""

    skill_name: str
    output_path: Path
    size_bytes: int
    file_count: int


_DEFAULT_EXCLUDES = (
    "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules",
    ".git", ".DS_Store",
)


def pack_skill(
    source: Path,
    *,
    output: Path | None = None,
    excludes: Iterable[str] = _DEFAULT_EXCLUDES,
) -> PublishResult:
    """Tar-gz a local skill directory into a sharable bundle.

    The tarball contains exactly one top-level directory named
    after the source dir — that's what ``LocalSource.fetch``
    expects on the install side. Bundles produced here drop in
    cleanly when someone runs ``korpha skill install
    ./bundle.tar.gz``.

    ``output`` defaults to ``./<source_name>.tar.gz`` next to
    the caller's current working dir. Excludes default to the
    usual junk (cache dirs, vendored deps, .git) so a packed
    skill stays small even if the author works inside a venv.

    Returns the path + size + file count for the CLI to show.
    """
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(
            f"pack_skill: source {source} is not a directory",
        )
    if not (source / "manifest.yaml").is_file():
        raise ValueError(
            f"pack_skill: {source} is missing a manifest.yaml; "
            "doesn't look like an Korpha skill",
        )

    skill_name = source.name
    if output is None:
        output = Path.cwd() / f"{skill_name}.tar.gz"
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    excludes_set = set(excludes)
    file_count = 0

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = info.name.split("/", 1)
        # rel[0] is always the top-level skill dir name; rel[1]
        # (if present) is the path inside it. We exclude any
        # path component matching the excludes set.
        parts = info.name.split("/")
        if any(p in excludes_set for p in parts):
            return None
        return info

    with tarfile.open(output, "w:gz", compresslevel=6) as tar:
        # Add the skill dir under its own basename so the
        # extracted tree has a single top-level dir matching
        # ``source.name``.
        before = file_count
        for entry in sorted(source.rglob("*")):
            try:
                rel = entry.relative_to(source)
            except ValueError:
                continue
            parts = rel.parts
            if any(p in excludes_set for p in parts):
                continue
            arcname = f"{skill_name}/{rel}" if parts else skill_name
            try:
                tar.add(entry, arcname=arcname, recursive=False)
            except OSError as exc:
                logger.warning(
                    "pack_skill: skip %s: %s", entry, exc,
                )
                continue
            if entry.is_file():
                file_count += 1

        if file_count == before:
            # The dir was empty after excludes — surface this as
            # an error rather than producing a useless tarball.
            output.unlink(missing_ok=True)
            raise ValueError(
                f"pack_skill: no publishable files in {source} "
                f"after excludes {sorted(excludes_set)}",
            )

    return PublishResult(
        skill_name=skill_name,
        output_path=output,
        size_bytes=output.stat().st_size,
        file_count=file_count,
    )


__all__ = [
    "LocalSource",
    "PublishResult",
    "pack_skill",
]
