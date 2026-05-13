"""Skill curator — archive stale agent-authored skills.

Why: ``meta.author_skill`` / ``meta.author_python_skill`` let the
agent generate skills indefinitely. After a few months the skill
catalog accumulates dead weight — half the skills are duplicates
or one-shots the founder never used again. Without curation:

  - The CEO router prompt grows linearly (each skill is in the
    catalog), so cold-start tokens climb.
  - The LLM picks worse skills (haystack effect — too many
    options blurs the right one).
  - Disk fills up with skills/agent_created/ entries.

Policy:

  - Only ``AGENT_AUTHORED`` skills are archive candidates. Built-ins
    + user-authored skills are exempt (the founder owns those
    decisions).
  - A skill is "stale" when ``last_invoked_at`` is older than
    ``stale_after_days`` (default 30) AND ``use_count`` is below
    ``min_uses`` (default 3). New skills get a grace period.
  - Pinned skills are exempt — the founder pins one when they want
    to keep it regardless of usage.
  - Archiving = tar.gz the skill source dir to
    ``~/.korpha/skills/archived/<id>.tar.gz`` + drop it from the
    registry. Restore is one CLI invocation.

Tracking is a JSON sidecar at ``~/.korpha/skills/_usage.json``
(global per-data-dir). Updated by a ``post_skill_call`` lifecycle
hook so usage tracking is invisible to skill authors.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from korpha.skills.registry import SkillRegistry, default_registry
from korpha.skills.types import SkillProvenance

logger = logging.getLogger(__name__)


DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_MIN_USES = 3
DEFAULT_GRACE_DAYS = 7
"""New skills get this many days where they can't be archived even
if unused — gives the founder time to discover they exist before
the curator scolds them off the stage."""


@dataclass
class SkillUsage:
    """Per-skill usage record. JSON-serialized in the sidecar."""

    skill_name: str
    use_count: int = 0
    last_invoked_at: float = 0.0
    """Unix epoch seconds. 0 = never."""

    first_seen_at: float = field(default_factory=time.time)
    pinned: bool = False
    """Founder-set: pinned skills are exempt from archiving."""

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "use_count": self.use_count,
            "last_invoked_at": self.last_invoked_at,
            "first_seen_at": self.first_seen_at,
            "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillUsage":
        return cls(
            skill_name=str(data.get("skill_name") or ""),
            use_count=int(data.get("use_count") or 0),
            last_invoked_at=float(data.get("last_invoked_at") or 0.0),
            first_seen_at=float(
                data.get("first_seen_at") or time.time()
            ),
            pinned=bool(data.get("pinned") or False),
        )


def _skills_dir() -> Path:
    env = os.environ.get("KORPHA_SKILLS_DIR")
    if env:
        return Path(env)
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        (Path(base) / "skills") if base
        else (Path.home() / ".korpha" / "skills")
    )


def _usage_path() -> Path:
    return _skills_dir() / "_usage.json"


def _archived_dir() -> Path:
    return _skills_dir() / "archived"


def load_usage() -> dict[str, SkillUsage]:
    """Read the usage sidecar. Empty dict if missing or corrupt."""
    path = _usage_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, SkillUsage] = {}
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        try:
            out[str(key)] = SkillUsage.from_dict(val)
        except (TypeError, ValueError):
            continue
    return out


def save_usage(usage: dict[str, SkillUsage]) -> None:
    """Atomically write the usage sidecar. Caller should serialize
    concurrent writes — we don't lock the file."""
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v.to_dict() for k, v in usage.items()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning("curator: usage save failed: %s", exc)


def record_invocation(skill_name: str) -> None:
    """Bump use_count + last_invoked_at for ``skill_name``. Called
    from the post_skill_call lifecycle hook."""
    usage = load_usage()
    rec = usage.get(skill_name) or SkillUsage(skill_name=skill_name)
    rec.use_count += 1
    rec.last_invoked_at = time.time()
    usage[skill_name] = rec
    save_usage(usage)


def install_usage_hook() -> None:
    """Wire the post_skill_call hook that bumps usage. Idempotent —
    re-installing is a no-op (only registers once per process)."""
    from korpha.plugins.hooks import (
        HookKind, PostSkillCallEvent, hook_registry,
    )

    # Avoid double-registration: check if our marker is already in
    # the listener list.
    for name, _ in hook_registry.listeners(HookKind.POST_SKILL_CALL):
        if name == "_curator_usage":
            return

    async def _bump(evt: PostSkillCallEvent) -> None:
        # Only bump on success — failed runs shouldn't count toward
        # "this skill is useful." Disputable; revisit if real usage
        # data shows we're over-archiving.
        if evt.succeeded:
            try:
                record_invocation(evt.skill_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("curator: bump failed: %s", exc)

    hook_registry.register(
        HookKind.POST_SKILL_CALL, _bump,
        plugin_name="_curator_usage",
    )


# ----------------------- archive / restore --------------------------


@dataclass(frozen=True)
class ArchiveCandidate:
    """A skill that the curator would archive on the next run.
    Returned by ``find_stale`` so callers can preview / dry-run
    before committing."""

    skill_name: str
    use_count: int
    last_invoked_at: float
    days_since_use: float
    reason: str


def find_stale(
    *,
    registry: SkillRegistry | None = None,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    min_uses: int = DEFAULT_MIN_USES,
    grace_days: int = DEFAULT_GRACE_DAYS,
    now: float | None = None,
) -> list[ArchiveCandidate]:
    """Scan the registry for ``AGENT_AUTHORED`` skills that meet the
    stale criteria. Returns candidates sorted by oldest-use first
    (so dry-runs show the most archive-worthy first)."""
    reg = registry if registry is not None else default_registry
    usage = load_usage()
    now = now if now is not None else time.time()
    candidates: list[ArchiveCandidate] = []

    stale_cutoff = now - (stale_after_days * 86400)
    grace_cutoff = now - (grace_days * 86400)

    for skill in reg.skills.values():
        if skill.spec.provenance != SkillProvenance.AGENT_AUTHORED:
            continue
        rec = usage.get(skill.spec.name)
        if rec is None:
            # Never invoked. Treat first_seen as "now" (just-discovered)
            # so the grace period applies; subsequent calls will see
            # the persisted record and judge against that timestamp.
            usage[skill.spec.name] = SkillUsage(skill_name=skill.spec.name)
            continue
        if rec.pinned:
            continue
        if rec.first_seen_at > grace_cutoff:
            continue  # still in grace period
        if rec.use_count >= min_uses and rec.last_invoked_at >= stale_cutoff:
            continue  # used enough recently
        days_since = (
            (now - rec.last_invoked_at) / 86400
            if rec.last_invoked_at > 0 else float("inf")
        )
        reason = (
            f"unused for {days_since:.0f} days, "
            f"{rec.use_count} use(s) total"
        )
        candidates.append(ArchiveCandidate(
            skill_name=skill.spec.name,
            use_count=rec.use_count,
            last_invoked_at=rec.last_invoked_at,
            days_since_use=days_since,
            reason=reason,
        ))
    save_usage(usage)
    candidates.sort(key=lambda c: c.last_invoked_at)
    return candidates


def archive_skill(skill_name: str) -> Path | None:
    """Tar.gz the skill's source dir + drop it from the registry.
    Returns the archive path on success, None if the skill couldn't
    be archived (not agent-authored, source not found, etc.).

    Doesn't validate that the skill is stale — that's the caller's
    job (find_stale + iterate). Allows the founder to manually
    archive a skill they don't like even if it's still being used.
    """
    skill = default_registry.skills.get(skill_name)
    if skill is None:
        logger.warning("curator: archive skill %s not in registry", skill_name)
        return None
    if skill.spec.provenance != SkillProvenance.AGENT_AUTHORED:
        logger.warning(
            "curator: refusing to archive non-agent-authored skill %s "
            "(provenance=%s)", skill_name, skill.spec.provenance.value,
        )
        return None

    src_dir = _resolve_skill_source(skill_name)
    if src_dir is None or not src_dir.exists():
        logger.warning(
            "curator: skill %s has no source dir to archive", skill_name,
        )
        return None

    archived = _archived_dir()
    archived.mkdir(parents=True, exist_ok=True)
    safe_id = skill_name.replace(".", "_").replace("/", "_")
    archive_path = archived / f"{safe_id}-{int(time.time())}.tar.gz"

    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(src_dir, arcname=safe_id)
    except (OSError, tarfile.TarError) as exc:
        logger.warning("curator: tar failed for %s: %s", skill_name, exc)
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    # Remove the source so it's not re-discovered on next loader run
    try:
        shutil.rmtree(src_dir)
    except OSError as exc:
        logger.warning(
            "curator: source removal failed for %s: %s; archive at %s",
            skill_name, exc, archive_path,
        )

    # Drop from registry. Drop usage record too — restoring should
    # start fresh (otherwise stale stats will re-archive immediately).
    default_registry.skills.pop(skill_name, None)
    usage = load_usage()
    usage.pop(skill_name, None)
    save_usage(usage)
    logger.info(
        "curator: archived skill %s to %s", skill_name, archive_path,
    )
    return archive_path


def _resolve_skill_source(skill_name: str) -> Path | None:
    """Best-effort guess at where the skill's source lives. We look
    at the conventional agent_created/{python,yaml}/<slug> shape
    used by ``meta.author_skill`` / ``author_python_skill``."""
    base = _skills_dir() / "agent_created"
    safe = skill_name.replace(".", "_")
    for sub in ("python", "yaml"):
        candidate = base / sub / safe
        if candidate.is_dir():
            return candidate
    # Some YAML loaders use the dotted name directly
    for sub in ("python", "yaml"):
        candidate = base / sub / skill_name
        if candidate.is_dir():
            return candidate
    return None


def list_archived() -> list[Path]:
    """Return tar.gz archive paths, newest first."""
    target = _archived_dir()
    if not target.is_dir():
        return []
    return sorted(
        target.glob("*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def restore_archived(archive_name: str) -> Path | None:
    """Extract an archived skill back to ``agent_created/``. The
    next ``load_agent_created_*_skills`` call picks it up. Caller
    should call the loader."""
    target = _archived_dir() / archive_name
    if not target.is_file():
        # Allow lookup by stem (founder may not have typed the .tar.gz)
        for path in list_archived():
            if path.stem == archive_name or path.name == archive_name:
                target = path
                break
    if not target.is_file():
        logger.warning("curator: archive %s not found", archive_name)
        return None
    extract_root = _skills_dir() / "agent_created"
    extract_root.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(target, "r:gz") as tar:
            # filter='data' = secure-by-default extraction (3.12+).
            tar.extractall(extract_root, filter="data")
    except (OSError, tarfile.TarError) as exc:
        logger.warning("curator: restore extract failed: %s", exc)
        return None
    return extract_root


def pin_skill(skill_name: str) -> bool:
    """Mark a skill as pinned — exempt from archiving regardless
    of usage. Returns True if the record was updated, False if the
    skill is unknown."""
    usage = load_usage()
    rec = usage.get(skill_name)
    if rec is None:
        rec = SkillUsage(skill_name=skill_name)
        usage[skill_name] = rec
    rec.pinned = True
    save_usage(usage)
    return True


def unpin_skill(skill_name: str) -> bool:
    """Remove the pin so the curator can consider the skill again."""
    usage = load_usage()
    rec = usage.get(skill_name)
    if rec is None:
        return False
    rec.pinned = False
    save_usage(usage)
    return True


__all__ = [
    "ArchiveCandidate",
    "DEFAULT_GRACE_DAYS",
    "DEFAULT_MIN_USES",
    "DEFAULT_STALE_AFTER_DAYS",
    "SkillUsage",
    "archive_skill",
    "find_stale",
    "install_usage_hook",
    "list_archived",
    "load_usage",
    "pin_skill",
    "record_invocation",
    "restore_archived",
    "save_usage",
    "unpin_skill",
]
