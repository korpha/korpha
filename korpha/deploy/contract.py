"""Deployer ABC + registry."""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(text: str, *, max_len: int = 60) -> str:
    """Turn arbitrary text into a deploy-safe slug. Lowercase
    alnum + dashes; collapses runs; bounded length."""
    s = text.strip().lower()
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len] or "site"


@dataclass(frozen=True)
class DeploymentTarget:
    """Normalized input. Skills construct one of these and hand
    it to whichever Deployer is active."""

    slug: str
    """Stable identifier — used as the subdir / subdomain. Auto-
    slugified if you pass arbitrary text via ``slug=``."""

    business_id: UUID
    files: Mapping[str, bytes | str] = field(default_factory=dict)
    """Path-relative-to-deploy-root → file contents. Strings get
    UTF-8 encoded; bytes pass through. ``index.html`` is
    required by every Deployer (default landing entry-point)."""

    title: str = ""
    """Human-readable name for the dashboard / approvals."""

    description: str = ""

    @classmethod
    def from_html(
        cls,
        *,
        business_id: UUID,
        slug: str,
        html: str,
        title: str = "",
        description: str = "",
        extras: Mapping[str, bytes | str] | None = None,
    ) -> "DeploymentTarget":
        """Convenience: most landing-page deployments are a single
        ``index.html`` + maybe a small style block. Pass them
        flatly here."""
        files: dict[str, bytes | str] = {"index.html": html}
        if extras:
            files.update(extras)
        return cls(
            slug=slugify(slug),
            business_id=business_id,
            files=files,
            title=title,
            description=description,
        )


@dataclass(frozen=True)
class DeploymentResult:
    """What the caller got back. ``url`` is the live link Mike
    clicks; everything else is metadata for the audit log +
    /app/kanban artifact emit."""

    url: str
    slug: str
    deployer_name: str
    bytes_written: int
    deployed_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    extra: dict = field(default_factory=dict)
    """Per-Deployer fields — Vercel deployment id, Cloudflare
    project id, etc. Don't depend on shape across implementations."""


class NoDeployerConfigured(RuntimeError):
    """Raised when ``deploy_registry.active()`` is called and
    no plugin / built-in has registered. Surfaced as a SkillError
    in the skill layer so Mike sees a clean message."""


class Deployer(ABC):
    """Abstract publish target."""

    name: str
    """Stable identifier ('local-file', 'vercel', 'cloudflare-pages').
    Used in logs + the audit trail."""

    @abstractmethod
    async def deploy(
        self, target: DeploymentTarget,
    ) -> DeploymentResult:
        """Push the files. Synchronous-feeling for the caller;
        Deployers running CLI tools should background them with
        asyncio.create_subprocess_exec."""

    @abstractmethod
    async def teardown(
        self, *, slug: str, business_id: UUID,
    ) -> bool:
        """Take down a previously-deployed slug. Returns True on
        success, False when the slug wasn't deployed by us."""


class _Registry:
    """Per-process singleton — one Deployer active at a time.
    Plugins call ``set_active_deployer`` during their
    register(host) step; the skill layer reads it via
    ``deploy_registry.active()``."""

    def __init__(self) -> None:
        self._active: Optional[Deployer] = None

    def set_active(
        self, deployer: Deployer, *, plugin_name: str = "",
    ) -> None:
        if self._active is not None and not isinstance(
            self._active, type(deployer)
        ):
            logger.warning(
                "deploy: replacing active deployer %r with %r "
                "(plugin %s)",
                self._active.name, deployer.name, plugin_name,
            )
        self._active = deployer

    def active(self) -> Deployer:
        if self._active is None:
            # Fall back to the built-in local default rather than
            # crashing the skill — Mike on a fresh install gets
            # a working preview without configuring a host.
            from korpha.deploy.local_file import LocalFileDeployer
            self._active = LocalFileDeployer()
        return self._active

    def reset(self) -> None:
        """Tests use this between cases."""
        self._active = None


deploy_registry = _Registry()


def set_active_deployer(
    deployer: Deployer, *, plugin_name: str = "",
) -> None:
    """Plugin convenience — same as ``deploy_registry.set_active``."""
    deploy_registry.set_active(deployer, plugin_name=plugin_name)


__all__ = [
    "Deployer",
    "DeploymentResult",
    "DeploymentTarget",
    "NoDeployerConfigured",
    "deploy_registry",
    "set_active_deployer",
    "slugify",
]
