"""Bridge between ExternalServiceAccount (PR4 per-unit creds) and the
existing InferencePool / ProviderAccount routing.

The InferencePool routes LLM calls against an in-memory pool of
``ProviderAccount`` dataclasses. PR4 introduced ``ExternalServiceAccount``
(persisted, per-unit-scopeable, capped). This bridge lets a calling
site opt into the per-unit path:

    creds = prefer_per_unit_credentials(ctx, ExternalServiceKind.LLM_ANTHROPIC, tier)
    if creds is not None:
        # Per-unit account resolved — use creds.account.credentials_encrypted
        api_key = decrypt(creds.account.credentials_encrypted)["api_key"]
        # ... make the API call directly ...
        record_call(session, account=creds.account, cost_usd=...)
    else:
        # No per-unit account configured — fall through to the legacy
        # InferencePool routing.
        response = await pool.complete(request)

PR-INT-3 ships the bridge function + tier-aware OAuth CLI integration.
A future PR replaces the InferencePool's account selection entirely
with this resolver; for now the bridge lets new call sites adopt
per-unit routing incrementally without breaking the existing path.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from korpha.audit.model import InferenceTier
from korpha.business_units.model import DeploymentMode
from korpha.credentials.model import ExternalServiceKind
from korpha.credentials.resolver import (
    NoCredentialsAvailable, ResolvedCredentials,
    current_deployment_mode, resolve_credentials,
)
from korpha.shared_resources.oauth_cli import (
    find_oauth_cli_for_service,
)

if TYPE_CHECKING:
    from korpha.shared_resources.model import SharedResource
    from korpha.skills.types import SkillContext


def prefer_per_unit_credentials(
    ctx: "SkillContext",
    service: ExternalServiceKind,
    tier: InferenceTier,
    *,
    deployment_mode: DeploymentMode | None = None,
) -> "ResolvedCredentials | SharedResource | None":
    """Return the best matching credential / shared resource for the
    calling agent, honoring tier + deployment mode rules.

    Returns:
      * ``SharedResource`` (OAuth CLI) — only for PRO tier in local
        mode when an OAuth CLI for this service is installed + quota
        is available. Caller invokes via subprocess plugin (#234).
      * ``ResolvedCredentials`` — a per-unit or company-default
        ExternalServiceAccount. Caller decrypts + uses directly.
      * ``None`` — no per-unit account configured. Caller should fall
        through to the legacy InferencePool routing.

    Tier rules (per ``BUSINESS_UNITS.md`` §Resolution):
      * PRO + local + OAuth CLI available + quota OK → OAuth CLI
      * PRO + local + no OAuth CLI / quota exhausted → API key walk
      * PRO + SaaS → API key walk only (no OAuth)
      * WORKHORSE → never OAuth, always API key walk

    Failures: NoCredentialsAvailable is caught + returned as None so
    the legacy fallback path runs. Other errors propagate.
    """
    mode = deployment_mode or current_deployment_mode()

    # PRO tier in local mode: prefer OAuth CLI shared resource.
    if tier == InferenceTier.PRO and mode == DeploymentMode.LOCAL:
        oauth = find_oauth_cli_for_service(
            ctx.session, service, deployment_mode=mode,
        )
        if oauth is not None:
            return oauth

    # API-key walk (works for both PRO with no OAuth + WORKHORSE).
    try:
        return resolve_credentials(
            ctx.session,
            business_unit_id=ctx.business_unit_id,
            business_id=ctx.business.id,
            service=service,
            deployment_mode=mode,
        )
    except NoCredentialsAvailable:
        return None


__all__ = ["prefer_per_unit_credentials"]
