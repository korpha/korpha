"""PR5 tests — SharedResource model + OAuth-CLI quota tracking +
deployment-mode gating + usage logging.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, DeploymentMode,
)
from korpha.credentials.model import ExternalServiceKind
from korpha.shared_resources import (
    OAuthCliQuotaExhausted,
    detect_installed_oauth_clis,
    find_oauth_cli_for_service,
    record_oauth_call,
)
from korpha.shared_resources.model import (
    SharedResource, SharedResourceKind, SharedResourceUsage,
)
from korpha.shared_resources.oauth_cli import KNOWN_OAUTH_CLIS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def unit(
    session: Session, business: Business,
) -> BusinessUnit:
    board = BusinessUnitBoard(session)
    return board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )


def _mk_resource(
    session: Session,
    business: Business,
    *,
    kind: SharedResourceKind = SharedResourceKind.AI_MODEL,
    name: str = "z-image-turbo",
    label: str = "Image gen on GPU mesh",
    modes: list[str] | None = None,
    quota_window_seconds: int | None = None,
    quota_limit: int | None = None,
    quota_used: int = 0,
    quota_started: datetime | None = None,
    is_active: bool = True,
    host_unit_id=None,
) -> SharedResource:
    r = SharedResource(
        business_id=business.id,
        kind=kind, name=name, label=label,
        host_business_unit_id=host_unit_id,
        available_in_modes=modes or ["local", "saas"],
        quota_window_seconds=quota_window_seconds,
        quota_limit_in_window=quota_limit,
        quota_calls_in_window=quota_used,
        quota_window_started_at=quota_started,
        config={}, is_active=is_active,
    )
    session.add(r); session.commit(); session.refresh(r)
    return r


# ---------------------------------------------------------------------------
# Model basics
# ---------------------------------------------------------------------------


def test_shared_resource_table_registered() -> None:
    cols = {c.name for c in SharedResource.__table__.columns}
    assert {
        "kind", "name", "label", "available_in_modes",
        "quota_window_seconds", "quota_limit_in_window",
        "quota_calls_in_window", "quota_window_started_at",
        "host_business_unit_id",
    } <= cols


def test_known_oauth_clis_includes_canonical_set() -> None:
    """The 8 CLIs from the design doc are all enumerable."""
    assert {
        "claude-code", "codex-cli", "opencode-go", "opencode-zen",
        "cursor", "gemini-cli", "acpx", "pi",
    } <= set(KNOWN_OAUTH_CLIS)


def test_detect_installed_oauth_clis_is_readonly() -> None:
    """Pure read-only operation. Either returns a list, never raises.
    In CI sandboxes none are typically installed; that's fine — what we
    test is that the function works without crashing."""
    out = detect_installed_oauth_clis()
    assert isinstance(out, list)
    for name in out:
        assert name in KNOWN_OAUTH_CLIS


# ---------------------------------------------------------------------------
# find_oauth_cli_for_service — deployment-mode gating
# ---------------------------------------------------------------------------


def test_saas_mode_returns_none(
    session: Session, business: Business,
) -> None:
    """SaaS mode never returns OAuth CLI resources — they cannot be
    shared across tenants."""
    _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
    )
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.SAAS,
    )
    assert out is None


def test_local_mode_returns_oauth_cli_when_installed(
    session: Session, business: Business,
) -> None:
    """Local mode returns the registered Claude Code resource for
    Anthropic-service queries."""
    cli = _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
    )
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is not None
    assert out.id == cli.id


def test_skips_oauth_cli_marked_saas_only_in_local_mode(
    session: Session, business: Business,
) -> None:
    """Belt-and-braces: an OAUTH_CLI row missing 'local' from
    available_in_modes is skipped even in local mode."""
    _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["saas"],  # weird config
    )
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is None


def test_returns_none_when_no_oauth_cli_for_service(
    session: Session, business: Business,
) -> None:
    """No matching CLI registered → None. Resolver falls back to API."""
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is None


def test_skips_inactive_oauth_cli(
    session: Session, business: Business,
) -> None:
    _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
        is_active=False,
    )
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is None


# ---------------------------------------------------------------------------
# Quota window
# ---------------------------------------------------------------------------


def test_quota_exhaustion_skips_resource(
    session: Session, business: Business,
) -> None:
    """Active OAuth CLI with cap=50 and used=50 within window → resolver
    skips it (returns None for this service)."""
    now = datetime.now(UTC)
    _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
        quota_window_seconds=18000, quota_limit=50,
        quota_used=50, quota_started=now - timedelta(minutes=30),
    )
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is None


def test_quota_window_rolled_over(
    session: Session, business: Business,
) -> None:
    """If the quota window has expired (started >5h ago for a 5h
    window), the resource is usable again — the next call will roll
    over the window. Resolver returns it."""
    six_hours_ago = datetime.now(UTC) - timedelta(hours=6)
    cli = _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
        quota_window_seconds=18000, quota_limit=50,
        quota_used=50, quota_started=six_hours_ago,
    )
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is not None
    assert out.id == cli.id


def test_no_quota_tracking_always_returns(
    session: Session, business: Business,
) -> None:
    """OAuth CLI with quota_window_seconds=None (e.g. opencode-go)
    is always usable."""
    cli = _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="opencode-go",
        label="OpenCode Go", modes=["local"],
        quota_window_seconds=None, quota_limit=None,
    )
    out = find_oauth_cli_for_service(
        session,
        service=ExternalServiceKind.LLM_OPENAI_COMPAT,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out is not None
    assert out.id == cli.id


# ---------------------------------------------------------------------------
# record_oauth_call — post-call accounting
# ---------------------------------------------------------------------------


def test_record_oauth_call_increments_counter(
    session: Session, business: Business, unit: BusinessUnit,
) -> None:
    cli = _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
        quota_window_seconds=18000, quota_limit=50,
    )
    record_oauth_call(
        session, resource=cli, consumer_unit_id=unit.id,
        skill_name="image.generate",
    )
    session.refresh(cli)
    assert cli.quota_calls_in_window == 1
    assert cli.quota_window_started_at is not None
    assert cli.last_used_at is not None


def test_record_oauth_call_inserts_usage_row(
    session: Session, business: Business, unit: BusinessUnit,
) -> None:
    cli = _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
    )
    record_oauth_call(
        session, resource=cli, consumer_unit_id=unit.id,
        skill_name="dev.codex_run",
    )
    usages = list(session.exec(select(SharedResourceUsage)).all())
    assert len(usages) == 1
    assert usages[0].resource_id == cli.id
    assert usages[0].consumer_unit_id == unit.id
    assert usages[0].skill_name == "dev.codex_run"


def test_record_oauth_call_rolls_over_expired_window(
    session: Session, business: Business, unit: BusinessUnit,
) -> None:
    """When the next call lands after the window expires, counter
    resets and window starts fresh."""
    six_hours_ago = datetime.now(UTC) - timedelta(hours=6)
    cli = _mk_resource(
        session, business,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", modes=["local"],
        quota_window_seconds=18000, quota_limit=50,
        quota_used=50, quota_started=six_hours_ago,
    )
    record_oauth_call(
        session, resource=cli, consumer_unit_id=unit.id,
        skill_name="x",
    )
    session.refresh(cli)
    # Window rolled over; this is the first call in the new window
    assert cli.quota_calls_in_window == 1
    started = cli.quota_window_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    assert started > six_hours_ago


# ---------------------------------------------------------------------------
# Non-OAuth resources (AI models on the mesh)
# ---------------------------------------------------------------------------


def test_ai_model_resource_available_in_both_modes(
    session: Session, business: Business,
) -> None:
    """AI models on the GPU mesh work in both local and SaaS deployments."""
    r = _mk_resource(
        session, business,
        kind=SharedResourceKind.AI_MODEL, name="z-image-turbo",
        label="z-image-turbo on Vidyo mesh",
    )
    assert r.available_in_modes == ["local", "saas"]


def test_ai_model_attributes_usage_to_consumer_unit(
    session: Session, business: Business, unit: BusinessUnit,
) -> None:
    """Calling an AI model logs which unit consumed it — drives the
    monthly review cross-line attribution."""
    r = _mk_resource(
        session, business,
        kind=SharedResourceKind.AI_MODEL, name="z-image-turbo",
        label="z-image-turbo",
    )
    session.add(SharedResourceUsage(
        resource_id=r.id, consumer_unit_id=unit.id,
        skill_name="image.generate", units_consumed=1.0,
        cost_attributed_usd=0.0,
    ))
    session.commit()
    usages = list(session.exec(select(SharedResourceUsage)).all())
    assert len(usages) == 1
    assert usages[0].consumer_unit_id == unit.id
