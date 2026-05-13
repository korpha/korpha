"""OAuth-authorized CLI handling for SharedResource.

These are the OAuth-bound CLI binaries that run on the founder's
machine: Claude Code, Codex CLI, OpenCode, Cursor, Gemini CLI, ACPX,
PI. The OAuth session is bound to the host machine — you cannot run
two installations with different OAuth identities. So they're
**company-wide singletons by physical constraint**, available in
local install only.

This module ships:

* The OAUTH_CLI ↔ service-kind mapping (Claude Code → Anthropic, etc.)
* Quota tracking (5-hour rolling windows for Claude.ai / ChatGPT Plus)
* Detection of which CLIs are installed on the host
* The pre-resolver hook the credentials resolver calls for Pro-tier
  work in local mode

The actual subprocess invocation lives in the inference adapter
plugins — this module is just the bookkeeping layer.
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from typing import Iterable

from sqlmodel import Session, select

from korpha.business_units.model import DeploymentMode
from korpha.credentials.model import ExternalServiceKind
from korpha.shared_resources.model import (
    SharedResource, SharedResourceKind, SharedResourceUsage,
)


class OAuthCliQuotaExhausted(Exception):
    """Raised by the resolver path when the only-available OAuth CLI
    for a service has burned its rolling-window quota. Caller falls
    through to the API-key path."""


# Mapping: which OAuth CLI binary serves which LLM service.
# Used by the resolver to find a candidate CLI when an agent wants
# Pro-tier inference in local mode.
_OAUTH_CLI_FOR_SERVICE: dict[ExternalServiceKind, tuple[str, ...]] = {
    ExternalServiceKind.LLM_ANTHROPIC: ("claude-code", "cursor"),
    ExternalServiceKind.LLM_OPENAI_COMPAT: ("codex-cli", "opencode-go"),
    ExternalServiceKind.LLM_GOOGLE: ("gemini-cli",),
}

# Binaries we know how to detect on disk. Mapping value is the
# subscription-quota shape — None means we don't track it.
KNOWN_OAUTH_CLIS: dict[str, dict[str, int | None | str]] = {
    "claude-code": {
        # Claude.ai Pro: rolling 5-hour window
        "quota_window_seconds": 18000,
        "quota_limit_in_window": 50,  # approximate; Anthropic varies
        "binary": "claude",
    },
    "codex-cli": {
        "quota_window_seconds": 18000,
        "quota_limit_in_window": 50,
        "binary": "codex",
    },
    "codex": {
        # Alias for "codex-cli" — operators sometimes register the
        # row under its bare binary name.
        "quota_window_seconds": 18000,
        "quota_limit_in_window": 50,
        "binary": "codex",
    },
    "opencode-go": {
        "quota_window_seconds": None,
        "quota_limit_in_window": None,
        "binary": "opencode-go",
    },
    "opencode-zen": {
        "quota_window_seconds": None,
        "quota_limit_in_window": None,
        "binary": "opencode-zen",
    },
    "cursor": {
        "quota_window_seconds": None,
        "quota_limit_in_window": None,
        "binary": "cursor",
    },
    "gemini-cli": {
        "quota_window_seconds": None,
        "quota_limit_in_window": None,
        "binary": "gemini",
    },
    "acpx": {
        "quota_window_seconds": None,
        "quota_limit_in_window": None,
        "binary": "acpx",
    },
    "pi": {
        "quota_window_seconds": None,
        "quota_limit_in_window": None,
        "binary": "pi",
    },
}


def resolve_oauth_cli_binary(resource_name: str) -> str:
    """Return the actual shell-executable command for a registered
    OAuth-CLI resource name. Falls back to the name itself when
    nothing's mapped, so plugin-registered CLIs keep working."""
    meta = KNOWN_OAUTH_CLIS.get(resource_name) or {}
    return str(meta.get("binary") or resource_name)


def detect_installed_oauth_clis() -> list[str]:
    """Return the binary names of OAuth CLIs found on $PATH.

    Used at startup + by ``korpha doctor`` to surface what's
    available. Pure read-only — never invokes the binary.
    """
    out: list[str] = []
    for name, meta in KNOWN_OAUTH_CLIS.items():
        binary = str(meta.get("binary") or name)
        if shutil.which(binary) is not None:
            out.append(name)
    return out


def find_oauth_cli_for_service(
    session: Session,
    service: ExternalServiceKind,
    *,
    deployment_mode: DeploymentMode,
) -> SharedResource | None:
    """Look up the active registered OAuth-CLI SharedResource that
    serves this LLM service. Returns None in SaaS mode (OAuth CLIs
    aren't enumerable there) or when none matches.

    Pro-tier resolution: this is called BEFORE per-unit API key
    resolution in local mode. If a CLI is available and its quota
    isn't burnt, return it. Otherwise return None and let the API
    fallback take over.
    """
    if deployment_mode == DeploymentMode.SAAS:
        return None

    candidates = _OAUTH_CLI_FOR_SERVICE.get(service, ())
    if not candidates:
        return None

    for cli_name in candidates:
        stmt = select(SharedResource).where(
            SharedResource.kind == SharedResourceKind.OAUTH_CLI,
            SharedResource.name == cli_name,
            SharedResource.is_active == True,  # noqa: E712
        )
        resource = session.exec(stmt).first()
        if resource is None:
            continue
        # Mode-gating belt-and-braces: even if SaaS install
        # registered an OAUTH_CLI row by mistake, skip it.
        if "local" not in (resource.available_in_modes or []):
            continue
        if _quota_exhausted(resource):
            continue
        return resource
    return None


def record_oauth_call(
    session: Session,
    *,
    resource: SharedResource,
    consumer_unit_id,
    skill_name: str | None = None,
) -> None:
    """Bump the rolling-window counter + insert a usage row.

    Rolls over the window if the current one has expired. Caller is
    responsible for raising OAuthCliQuotaExhausted *before* the API
    call if ``_quota_exhausted`` was true — this function only does
    the post-call accounting.
    """
    now = datetime.now(UTC)
    if resource.quota_window_started_at is None:
        resource.quota_window_started_at = now
        resource.quota_calls_in_window = 0
    elif resource.quota_window_seconds is not None:
        age = now - _ensure_aware(resource.quota_window_started_at)
        if age >= timedelta(seconds=resource.quota_window_seconds):
            # Window expired — roll over
            resource.quota_window_started_at = now
            resource.quota_calls_in_window = 0

    resource.quota_calls_in_window += 1
    resource.last_used_at = now
    session.add(resource)

    session.add(SharedResourceUsage(
        resource_id=resource.id,
        consumer_unit_id=consumer_unit_id,
        skill_name=skill_name,
        units_consumed=1.0,
        cost_attributed_usd=0.0,
    ))
    session.commit()


def _quota_exhausted(resource: SharedResource) -> bool:
    """True when the OAuth CLI's rolling-window quota is hit AND the
    window hasn't expired yet. False if no quota tracked."""
    if resource.quota_window_seconds is None:
        return False
    if resource.quota_limit_in_window is None:
        return False
    if resource.quota_window_started_at is None:
        return False
    now = datetime.now(UTC)
    age = now - _ensure_aware(resource.quota_window_started_at)
    if age >= timedelta(seconds=resource.quota_window_seconds):
        return False  # window expired; will roll over on next use
    return resource.quota_calls_in_window >= resource.quota_limit_in_window


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes. Force UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


__all__ = [
    "KNOWN_OAUTH_CLIS",
    "OAuthCliQuotaExhausted",
    "detect_installed_oauth_clis",
    "find_oauth_cli_for_service",
    "record_oauth_call",
]
