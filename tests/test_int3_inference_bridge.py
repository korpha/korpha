"""PR-INT-3 tests — prefer_per_unit_credentials bridge."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, DeploymentMode,
)
from korpha.credentials.inference_bridge import (
    prefer_per_unit_credentials,
)
from korpha.credentials.model import (
    ExternalServiceAccount, ExternalServiceKind,
)
from korpha.credentials.resolver import ResolvedCredentials
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.shared_resources.model import (
    SharedResource, SharedResourceKind,
)
from korpha.skills.types import SkillContext


@pytest.fixture
def default_unit(
    session: Session, business: Business,
) -> BusinessUnit:
    return BusinessUnitBoard(session).create(
        business_id=business.id, name=business.name,
        kind=BusinessUnitKind.DEFAULT,
    )


def _ctx(session, business, founder, unit_id=None):
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
        business_unit_id=unit_id,
    )


# ---------------------------------------------------------------------------
# Workhorse never uses OAuth CLI
# ---------------------------------------------------------------------------


def test_workhorse_skips_oauth_cli(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    """Even with OAuth CLI registered, Workhorse tier walks straight
    to ExternalServiceAccount path. Bulk work must not burn OAuth quota."""
    SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", available_in_modes=["local"],
        config={}, is_active=True,
    )
    # Add per-unit Anthropic API key
    acc = ExternalServiceAccount(
        business_id=business.id,
        business_unit_id=default_unit.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        label="anthropic", credentials_encrypted=b"<x>",
        is_active=True,
    )
    session.add(acc); session.commit(); session.refresh(acc)

    out = prefer_per_unit_credentials(
        _ctx(session, business, founder, default_unit.id),
        ExternalServiceKind.LLM_ANTHROPIC,
        InferenceTier.WORKHORSE,
        deployment_mode=DeploymentMode.LOCAL,
    )
    # Returned a ResolvedCredentials (API), NOT a SharedResource (OAuth)
    assert isinstance(out, ResolvedCredentials)
    assert out.account.id == acc.id


# ---------------------------------------------------------------------------
# PRO + local + OAuth CLI present → returns OAuth resource
# ---------------------------------------------------------------------------


def test_pro_local_returns_oauth_cli_when_available(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    cli = SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", available_in_modes=["local"],
        config={}, is_active=True,
    )
    session.add(cli); session.commit(); session.refresh(cli)

    out = prefer_per_unit_credentials(
        _ctx(session, business, founder, default_unit.id),
        ExternalServiceKind.LLM_ANTHROPIC,
        InferenceTier.PRO,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert isinstance(out, SharedResource)
    assert out.id == cli.id


# ---------------------------------------------------------------------------
# PRO + SaaS → no OAuth path
# ---------------------------------------------------------------------------


def test_pro_saas_skips_oauth(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    cli = SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", available_in_modes=["local"],
        config={}, is_active=True,
    )
    session.add(cli); session.commit()
    acc = ExternalServiceAccount(
        business_id=business.id,
        business_unit_id=default_unit.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        label="x", credentials_encrypted=b"<x>", is_active=True,
    )
    session.add(acc); session.commit(); session.refresh(acc)

    out = prefer_per_unit_credentials(
        _ctx(session, business, founder, default_unit.id),
        ExternalServiceKind.LLM_ANTHROPIC,
        InferenceTier.PRO,
        deployment_mode=DeploymentMode.SAAS,
    )
    # SaaS mode → straight to API account
    assert isinstance(out, ResolvedCredentials)
    assert out.account.id == acc.id


# ---------------------------------------------------------------------------
# OAuth exhausted → falls through to API
# ---------------------------------------------------------------------------


def test_pro_local_falls_through_when_oauth_exhausted(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    cli = SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", available_in_modes=["local"],
        config={}, is_active=True,
        quota_window_seconds=18000, quota_limit_in_window=50,
        quota_calls_in_window=50,
        quota_window_started_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    session.add(cli); session.commit()
    acc = ExternalServiceAccount(
        business_id=business.id,
        business_unit_id=default_unit.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        label="x", credentials_encrypted=b"<x>", is_active=True,
    )
    session.add(acc); session.commit(); session.refresh(acc)

    out = prefer_per_unit_credentials(
        _ctx(session, business, founder, default_unit.id),
        ExternalServiceKind.LLM_ANTHROPIC,
        InferenceTier.PRO,
        deployment_mode=DeploymentMode.LOCAL,
    )
    # OAuth quota burned → API key wins
    assert isinstance(out, ResolvedCredentials)


# ---------------------------------------------------------------------------
# No per-unit, no OAuth → None (caller falls back to legacy pool)
# ---------------------------------------------------------------------------


def test_no_per_unit_returns_none_for_legacy_fallback(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    out = prefer_per_unit_credentials(
        _ctx(session, business, founder, default_unit.id),
        ExternalServiceKind.LLM_ANTHROPIC,
        InferenceTier.WORKHORSE,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is None
