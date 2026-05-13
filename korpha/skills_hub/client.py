"""Skills hub client — install skills from external registries.

Adapted from Hermes Agent (MIT, Nous Research) — ``hermes/tools/skills_hub.py``.
The source-adapter ABC, lock-file shape, and quarantine-then-scan-then-
install flow are direct ports with attribution. Korpha extends with:

  - ``KorphaHubSource`` — pulls from skills.korpha.com (the
    official registry we run + the open-source v1 marketplace)
  - Cofounder-protocol awareness — installs that ship a manifest
    register the partner via ``korpha.protocol`` automatically
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    """Resolve ~/.korpha (overridable via ``KORPHA_DATA_DIR``)."""
    override = os.getenv("KORPHA_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".korpha"


def skills_dir() -> Path:
    return _data_dir() / "skills"


def hub_dir() -> Path:
    return skills_dir() / ".hub"


def quarantine_dir() -> Path:
    """Where downloads land before scanning. Skills only move out
    after passing the security scanner + install policy check."""
    return hub_dir() / "quarantine"


def lock_file() -> Path:
    return hub_dir() / "lock.json"


def audit_log() -> Path:
    return hub_dir() / "audit.log"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillMeta:
    """Minimal metadata returned by source.search() / source.list()."""

    name: str
    description: str
    source: str           # "korpha" | "github:owner/repo" | etc.
    identifier: str       # source-specific, fed back to source.fetch()
    trust_level: str      # "builtin" | "trusted" | "community"
    repo: str | None = None
    path: str | None = None
    tags: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillBundle:
    """A downloaded skill ready for scanning + installation."""

    name: str
    source: str
    identifier: str
    quarantine_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstallResult:
    """Outcome of an install attempt — success, blocked, or skipped."""

    name: str
    installed: bool
    install_path: Path | None
    reason: str
    scan_report: str = ""


# ---------------------------------------------------------------------------
# Source adapter ABC
# ---------------------------------------------------------------------------


class SkillSource(ABC):
    """Abstract interface for any skill registry adapter.

    Implementations:
      - ``KorphaHubSource`` — the official Korpha registry
      - ``GitHubSource``       — generic GitHub repo (any path)
      - ``LocalSource``        — local filesystem (for testing)
    """

    name: str = "unnamed"

    @abstractmethod
    def search(self, query: str = "", *, limit: int = 50) -> list[SkillMeta]: ...

    @abstractmethod
    def fetch(self, identifier: str) -> SkillBundle:
        """Download skill into the quarantine dir + return the bundle."""


# ---------------------------------------------------------------------------
# Korpha Hub source — the canonical registry
# ---------------------------------------------------------------------------


@dataclass
class KorphaHubSource(SkillSource):
    """Pulls from ``skills.korpha.com`` (or whatever you set
    ``KORPHA_SKILLS_HUB_URL`` to — useful for self-hosted hubs +
    development)."""

    base_url: str = field(
        default_factory=lambda: os.getenv(
            "KORPHA_SKILLS_HUB_URL", "https://skills.korpha.com"
        )
    )
    timeout_seconds: float = 30.0
    name: str = "korpha"

    def search(self, query: str = "", *, limit: int = 50) -> list[SkillMeta]:
        import httpx

        params: dict[str, str | int] = (
            {"q": query, "limit": limit} if query else {"limit": limit}
        )
        url = f"{self.base_url.rstrip('/')}/api/v1/skills"
        try:
            resp = httpx.get(url, params=params, timeout=self.timeout_seconds)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Korpha hub search failed: %s", exc)
            return []
        body = resp.json()
        out: list[SkillMeta] = []
        for entry in body.get("skills", []):
            out.append(SkillMeta(
                name=str(entry.get("name", "")),
                description=str(entry.get("description", "")),
                source="korpha",
                identifier=str(entry.get("name", "")),
                trust_level=str(entry.get("trust_level", "community")),
                repo=entry.get("repo"),
                path=entry.get("path"),
                tags=tuple(entry.get("tags", [])),
                extra={
                    "verified": bool(entry.get("verified")),
                    "scan_verdict": entry.get("scan_verdict"),
                    "installs": entry.get("installs", 0),
                },
            ))
        return out

    def fetch(self, identifier: str) -> SkillBundle:
        import httpx

        url = f"{self.base_url.rstrip('/')}/api/v1/skills/{identifier}/download"
        target = quarantine_dir() / identifier
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)

        try:
            resp = httpx.get(url, timeout=self.timeout_seconds, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Couldn't download {identifier} from Korpha hub: {exc}"
            ) from exc

        target.mkdir(parents=True)
        # The hub serves a tarball; expand to disk.
        import io
        import tarfile

        try:
            with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
                # Safe extract — reject path traversal + absolute paths
                for member in tar.getmembers():
                    name = member.name
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise RuntimeError(
                            f"unsafe member path in tarball: {name}"
                        )
                # Pass filter='data' (Python 3.12+) to enforce safe-extract
                # rules at tarfile level — defense-in-depth on top of our
                # own member-name check above.
                tar.extractall(target, filter="data")
        except tarfile.TarError as exc:
            raise RuntimeError(
                f"Couldn't unpack {identifier} tarball: {exc}"
            ) from exc

        return SkillBundle(
            name=identifier,
            source="korpha",
            identifier=identifier,
            quarantine_path=target,
        )


# ---------------------------------------------------------------------------
# GitHub source — any repo, any path
# ---------------------------------------------------------------------------


@dataclass
class GitHubSource(SkillSource):
    """Generic adapter for any GitHub repo containing SKILL.md files.

    Trust level depends on the repo identifier — see
    ``guard.TRUSTED_REPOS``. Install policy applies accordingly."""

    repo: str
    """e.g. ``"openai/skills"`` or ``"NousResearch/hermes-agent"``."""

    base_path: str = ""
    """Subdirectory inside the repo where skills live. Empty = root."""

    branch: str = "main"
    timeout_seconds: float = 30.0

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"github:{self.repo}"

    def search(self, query: str = "", *, limit: int = 50) -> list[SkillMeta]:
        """List skills by browsing the repo's directory contents.

        We use the GitHub Contents API rather than git clone — fast,
        no working copy, no auth required for public repos.
        """
        import httpx

        url = f"https://api.github.com/repos/{self.repo}/contents/{self.base_path}"
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = httpx.get(url, headers=headers, params={"ref": self.branch},
                             timeout=self.timeout_seconds)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("GitHub source list failed for %s: %s", self.repo, exc)
            return []

        body = resp.json()
        if not isinstance(body, list):
            return []
        out: list[SkillMeta] = []
        q = query.lower()
        for entry in body[:limit]:
            if entry.get("type") != "dir":
                continue
            name = str(entry.get("name", ""))
            if q and q not in name.lower():
                continue
            out.append(SkillMeta(
                name=name,
                description=f"Skill from {self.repo}",
                source=self.name,
                identifier=f"{self.base_path}/{name}".strip("/"),
                trust_level="community",  # resolved properly by guard later
                repo=self.repo,
                path=f"{self.base_path}/{name}".strip("/"),
            ))
        return out

    def fetch(self, identifier: str) -> SkillBundle:
        """Download a directory tree from GitHub via the Contents API.

        Recursive — fetches every file in the skill dir to the
        quarantine area. This is enough for the static scanner to do
        its job."""
        import httpx

        target = quarantine_dir() / Path(identifier).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)

        token = os.getenv("GITHUB_TOKEN")
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        def _walk(path: str, dest: Path) -> None:
            url = f"https://api.github.com/repos/{self.repo}/contents/{path}"
            resp = httpx.get(url, headers=headers, params={"ref": self.branch},
                             timeout=self.timeout_seconds, follow_redirects=True)
            resp.raise_for_status()
            entries = resp.json()
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries:
                e_type = entry.get("type")
                e_name = entry.get("name", "")
                if e_type == "file":
                    download_url = entry.get("download_url")
                    if not download_url:
                        continue
                    raw = httpx.get(download_url, headers=headers,
                                    timeout=self.timeout_seconds, follow_redirects=True)
                    raw.raise_for_status()
                    (dest / e_name).write_bytes(raw.content)
                elif e_type == "dir":
                    sub = dest / e_name
                    sub.mkdir(exist_ok=True)
                    _walk(entry.get("path", f"{path}/{e_name}"), sub)

        try:
            _walk(identifier, target)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Couldn't fetch {identifier} from {self.repo}: {exc}"
            ) from exc

        return SkillBundle(
            name=Path(identifier).name,
            source=self.name,
            identifier=identifier,
            quarantine_path=target,
        )


# ---------------------------------------------------------------------------
# Lock file — track what we installed + from where
# ---------------------------------------------------------------------------


@dataclass
class HubLockFile:
    """Maps installed skill name → provenance.

    ``~/.korpha/skills/.hub/lock.json``. JSON-only, hand-readable,
    git-friendly. Lets ``korpha skill hub-list`` show what came
    from where.
    """

    path: Path = field(default_factory=lock_file)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data: dict[str, dict[str, Any]] = json.loads(
                self.path.read_text(encoding="utf-8")
            )
            return data
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, entries: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8"
        )

    def record(self, skill_name: str, source: str, identifier: str,
               *, sha256: str, scan_verdict: str) -> None:
        entries = self.load()
        entries[skill_name] = {
            "source": source,
            "identifier": identifier,
            "sha256": sha256,
            "scan_verdict": scan_verdict,
            "installed_at": datetime.now(UTC).isoformat(),
        }
        self.save(entries)

    def remove(self, skill_name: str) -> bool:
        entries = self.load()
        if skill_name not in entries:
            return False
        del entries[skill_name]
        self.save(entries)
        return True


# ---------------------------------------------------------------------------
# Install flow — fetch, scan, decide, install
# ---------------------------------------------------------------------------


def install_skill(
    bundle: SkillBundle,
    *,
    force: bool = False,
    target_dir: Path | None = None,
) -> InstallResult:
    """Run the security scanner on a downloaded bundle, apply install
    policy, copy to ``~/.korpha/skills/<name>`` if allowed, record
    in lock file.

    Returns InstallResult — caller surfaces ``installed=False`` to the
    user with the scan report attached.
    """
    from korpha.skills_hub.guard import (
        content_hash,
        format_scan_report,
        scan_skill,
        should_allow_install,
    )

    scan = scan_skill(bundle.quarantine_path, source=bundle.source)
    decision, reason = should_allow_install(scan, force=force)
    report = format_scan_report(scan)

    if decision is None:
        # 'ask' — caller must surface confirmation. We don't prompt
        # from this layer (would require interactive mode); return a
        # special result that the CLI / dashboard handles.
        return InstallResult(
            name=bundle.name,
            installed=False,
            install_path=None,
            reason=f"NEEDS CONFIRMATION: {reason}",
            scan_report=report,
        )

    if decision is False:
        return InstallResult(
            name=bundle.name,
            installed=False,
            install_path=None,
            reason=f"BLOCKED: {reason}",
            scan_report=report,
        )

    target = target_dir or skills_dir() / bundle.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(bundle.quarantine_path, target)

    HubLockFile().record(
        skill_name=bundle.name,
        source=bundle.source,
        identifier=bundle.identifier,
        sha256=content_hash(target),
        scan_verdict=scan.verdict,
    )

    _append_audit(
        f"installed {bundle.name} from {bundle.source} "
        f"(verdict={scan.verdict}, force={force})"
    )

    return InstallResult(
        name=bundle.name,
        installed=True,
        install_path=target,
        reason=reason,
        scan_report=report,
    )


def uninstall_skill(skill_name: str) -> bool:
    """Remove an installed hub skill from disk + lock file."""
    target = skills_dir() / skill_name
    if not target.exists():
        return False
    shutil.rmtree(target)
    HubLockFile().remove(skill_name)
    _append_audit(f"uninstalled {skill_name}")
    return True


def list_installed() -> list[dict[str, Any]]:
    """Return every hub-installed skill with its provenance."""
    entries = HubLockFile().load()
    return [{"name": name, **meta} for name, meta in sorted(entries.items())]


def _append_audit(message: str) -> None:
    log = audit_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{ts}  {message}\n")


__all__ = [
    "KorphaHubSource",
    "GitHubSource",
    "HubLockFile",
    "InstallResult",
    "SkillBundle",
    "SkillMeta",
    "SkillSource",
    "audit_log",
    "hub_dir",
    "install_skill",
    "list_installed",
    "lock_file",
    "quarantine_dir",
    "skills_dir",
    "uninstall_skill",
]


# Quiet unused-import in static analysis (asdict is used by the
# JSON-serialization path in callers).
_ = asdict
