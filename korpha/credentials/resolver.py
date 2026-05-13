"""Tier + deployment-mode-aware hierarchical credential resolver.

Algorithm (per ``BUSINESS_UNITS.md`` Resolution section):

  SaaS mode → only API keys; no OAuth fallback exists.
  Local mode + PRO tier → prefer OAuth CLI shared resource (PR5), then
    walk the unit tree for an API key.
  Local mode + WORKHORSE tier → walk the unit tree directly for an API
    key. NEVER OAuth CLI — bulk work burns subscription quota.

For each tier-walk step:
  1. Look for account scoped to calling unit → use if active+uncapped.
  2. Walk up to parent unit → repeat.
  3. Fall through to company-wide (business_unit_id IS NULL) → use.
  4. None found → raise NoCredentialsAvailable.

PR4 ships the resolver without the OAuth CLI integration; the
SharedResource OAuth-CLI handling lands in PR5 which adds a hook here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.business_units.model import (
    BusinessUnit, DeploymentMode,
)
from korpha.credentials.model import (
    ExternalServiceAccount, ExternalServiceKind,
)


class NoCredentialsAvailable(Exception):
    """Raised when no account matches the request — neither per-unit
    nor company-wide. Caller surfaces this as a setup-wizard prompt
    (founder action required) or a hard error in non-interactive
    contexts."""


@dataclass(frozen=True)
class ResolvedCredentials:
    """What the resolver returns. ``account`` is the row; ``source``
    distinguishes per-unit vs company-default for the call site to
    log + attribute usage correctly."""

    account: ExternalServiceAccount
    source: str  # "unit:<uuid>" | "company_default"


def current_deployment_mode() -> DeploymentMode:
    """Read deployment mode from env. Cached per process.

    SaaS mode flips the resolver's OAuth-CLI consideration off; in
    production this is set once at deploy time. Test code uses
    monkeypatch to override.
    """
    raw = (os.environ.get("KORPHA_DEPLOYMENT_MODE") or "local").lower()
    if raw == "saas":
        return DeploymentMode.SAAS
    return DeploymentMode.LOCAL


def resolve_credentials(
    session: Session,
    *,
    business_unit_id: UUID | None,
    business_id: UUID,
    service: ExternalServiceKind,
    deployment_mode: DeploymentMode | None = None,
) -> ResolvedCredentials:
    """Walk the unit tree leaf-to-root for the best matching account.

    ``business_unit_id=None`` means the caller is not unit-scoped (e.g.
    a system-level cron job); we go straight to the company-wide
    fallback for the business.

    ``deployment_mode`` defaults to ``current_deployment_mode()`` —
    pass explicitly in tests.
    """
    if deployment_mode is None:
        deployment_mode = current_deployment_mode()

    # SaaS mode hook for future OAuth CLI exclusion at the source-side;
    # for v1 the resolver only consults ExternalServiceAccount, so
    # SaaS vs LOCAL doesn't change anything in this function yet.
    # PR5's OAuth CLI integration adds a pre-step that consults
    # SharedResource OAUTH_CLI rows BEFORE this resolver in local mode
    # for PRO tier work.
    _ = deployment_mode  # reserved for future use

    # 1+2. Walk the unit tree, leaf to root.
    if business_unit_id is not None:
        cursor: UUID | None = business_unit_id
        seen: set[UUID] = set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            account = _find_account(
                session,
                business_unit_id=cursor,
                business_id=business_id,
                service=service,
            )
            if account is not None and not _cap_exhausted(account):
                return ResolvedCredentials(
                    account=account, source=f"unit:{cursor}",
                )
            # Walk up
            unit = session.get(BusinessUnit, cursor)
            cursor = unit.parent_id if unit is not None else None

    # 3. Company-wide fallback (business_unit_id IS NULL).
    account = _find_account(
        session,
        business_unit_id=None,
        business_id=business_id,
        service=service,
    )
    if account is not None and not _cap_exhausted(account):
        return ResolvedCredentials(
            account=account, source="company_default",
        )

    # 4. Nothing found.
    raise NoCredentialsAvailable(
        f"No {service.value} account available for unit "
        f"{business_unit_id} (business {business_id}). Run "
        f"`korpha credentials set` or configure via dashboard."
    )


def _find_account(
    session: Session,
    *,
    business_unit_id: UUID | None,
    business_id: UUID,
    service: ExternalServiceKind,
) -> ExternalServiceAccount | None:
    """One-step lookup — does NOT recurse. Resolver does the recursion."""
    stmt = select(ExternalServiceAccount).where(
        ExternalServiceAccount.business_id == business_id,
        ExternalServiceAccount.service == service,
        ExternalServiceAccount.is_active == True,  # noqa: E712
    )
    if business_unit_id is None:
        stmt = stmt.where(
            ExternalServiceAccount.business_unit_id.is_(None)  # type: ignore[union-attr]
        )
    else:
        stmt = stmt.where(
            ExternalServiceAccount.business_unit_id == business_unit_id
        )
    return session.exec(stmt).first()


def _cap_exhausted(account: ExternalServiceAccount) -> bool:
    """True if the monthly spending cap is hit. Resolver skips this
    account and moves to the next-up parent."""
    if account.spending_cap_usd_per_month is None:
        return False
    return (
        account.spending_used_this_month_usd
        >= account.spending_cap_usd_per_month
    )


def record_call(
    session: Session,
    *,
    account: ExternalServiceAccount,
    cost_usd: Decimal | float,
) -> None:
    """Post-call accounting. Increments spent_this_month and trips
    cap-exhausted alerts. Idempotency on retries is the caller's job
    — this helper just bumps the counter and commits."""
    from datetime import UTC, datetime

    cost = Decimal(str(cost_usd))
    account.spending_used_this_month_usd = (
        account.spending_used_this_month_usd + cost
    )
    account.last_used_at = datetime.now(UTC)
    session.add(account)
    session.commit()


__all__ = [
    "NoCredentialsAvailable",
    "ResolvedCredentials",
    "current_deployment_mode",
    "record_call",
    "resolve_credentials",
]
