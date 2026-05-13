"""PR4 tests — tier+mode-aware hierarchical credential resolver.

Resolver walks a unit tree leaf → root looking for matching active
uncapped accounts. Falls through to company-wide (NULL unit_id). Tests
cover: 5-fixture tree-walk pattern, cap exhaustion, multi-service
isolation, SaaS vs local mode, missing-credential error.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, DeploymentMode,
)
from korpha.credentials.model import (
    ExternalServiceAccount, ExternalServiceKind,
)
from korpha.credentials.resolver import (
    NoCredentialsAvailable, current_deployment_mode,
    record_call, resolve_credentials,
)


# ---------------------------------------------------------------------------
# Tree fixture — 3 levels deep
# ---------------------------------------------------------------------------


@pytest.fixture
def tree(
    session: Session, business: Business,
) -> dict[str, BusinessUnit]:
    """Marketro → KDP → Romance, 3 levels."""
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    kdp = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    romance = board.create(
        business_id=business.id, name="Romance",
        kind=BusinessUnitKind.TYPE, parent_id=kdp.id,
    )
    return {"root": root, "kdp": kdp, "romance": romance}


def _mk_account(
    session: Session,
    *,
    business: Business,
    unit_id=None,
    service=ExternalServiceKind.LLM_ANTHROPIC,
    label="acc",
    cap=None,
    used=Decimal("0"),
    active=True,
) -> ExternalServiceAccount:
    acc = ExternalServiceAccount(
        business_id=business.id,
        business_unit_id=unit_id,
        service=service,
        label=label,
        credentials_encrypted=b"<encrypted>",
        spending_cap_usd_per_month=cap,
        spending_used_this_month_usd=used,
        is_active=active,
    )
    session.add(acc); session.commit(); session.refresh(acc)
    return acc


# ---------------------------------------------------------------------------
# 5-fixture tree walk
# ---------------------------------------------------------------------------


def test_resolver_finds_leaf_account_first(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Account on the calling unit wins even when ancestors have one."""
    company = _mk_account(
        session, business=business, label="company",
    )
    kdp_acc = _mk_account(
        session, business=business, unit_id=tree["kdp"].id, label="kdp",
    )
    leaf = _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="romance",
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == leaf.id
    assert out.source == f"unit:{tree['romance'].id}"


def test_resolver_walks_up_to_parent(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """No leaf account → resolver picks the parent's."""
    _mk_account(session, business=business, label="company")
    parent_acc = _mk_account(
        session, business=business, unit_id=tree["kdp"].id, label="kdp",
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == parent_acc.id
    assert out.source == f"unit:{tree['kdp'].id}"


def test_resolver_walks_to_grandparent(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    root_acc = _mk_account(
        session, business=business, unit_id=tree["root"].id, label="root",
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == root_acc.id


def test_resolver_falls_through_to_company_default(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """No unit accounts anywhere in the chain → company default."""
    company = _mk_account(
        session, business=business, label="company-wide",
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == company.id
    assert out.source == "company_default"


def test_resolver_raises_when_no_account_anywhere(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    with pytest.raises(NoCredentialsAvailable, match="No llm_anthropic"):
        resolve_credentials(
            session,
            business_unit_id=tree["romance"].id,
            business_id=business.id,
            service=ExternalServiceKind.LLM_ANTHROPIC,
            deployment_mode=DeploymentMode.LOCAL,
        )


# ---------------------------------------------------------------------------
# Cap exhaustion
# ---------------------------------------------------------------------------


def test_resolver_skips_exhausted_account_and_promotes(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Romance has a capped-out account → resolver walks up to KDP."""
    _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="romance-capped",
        cap=Decimal("100"), used=Decimal("100"),
    )
    kdp_acc = _mk_account(
        session, business=business, unit_id=tree["kdp"].id,
        label="kdp-fresh",
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == kdp_acc.id


def test_resolver_uses_account_with_room_under_cap(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Cap not yet hit → still usable."""
    acc = _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="romance-half-used",
        cap=Decimal("100"), used=Decimal("50"),
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == acc.id


def test_resolver_skips_inactive_account(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="disabled", active=False,
    )
    kdp_acc = _mk_account(
        session, business=business, unit_id=tree["kdp"].id, label="kdp",
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == kdp_acc.id


# ---------------------------------------------------------------------------
# Service isolation — different services don't collide
# ---------------------------------------------------------------------------


def test_resolver_distinguishes_services(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Romance has Anthropic but no Stripe — Stripe falls through."""
    _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="ant", service=ExternalServiceKind.LLM_ANTHROPIC,
    )
    stripe = _mk_account(
        session, business=business,
        label="stripe-co", service=ExternalServiceKind.STRIPE,
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.STRIPE,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == stripe.id


# ---------------------------------------------------------------------------
# Deployment-mode plumbing
# ---------------------------------------------------------------------------


def test_resolver_works_in_saas_mode(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """SaaS mode still resolves API-key accounts identically. The
    SaaS-specific OAuth-CLI exclusion lands in PR5."""
    acc = _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="ant",
    )
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.SAAS,
    )
    assert out.account.id == acc.id


def test_current_deployment_mode_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DEPLOYMENT_MODE", "saas")
    assert current_deployment_mode() == DeploymentMode.SAAS
    monkeypatch.setenv("KORPHA_DEPLOYMENT_MODE", "local")
    assert current_deployment_mode() == DeploymentMode.LOCAL
    monkeypatch.delenv("KORPHA_DEPLOYMENT_MODE", raising=False)
    assert current_deployment_mode() == DeploymentMode.LOCAL


def test_null_business_unit_id_goes_to_company_default(
    session: Session, business: Business,
) -> None:
    """Non-unit-scoped caller (system cron, etc.) → straight to
    company default."""
    company = _mk_account(
        session, business=business, label="company-only",
    )
    out = resolve_credentials(
        session,
        business_unit_id=None,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == company.id
    assert out.source == "company_default"


# ---------------------------------------------------------------------------
# record_call accounting
# ---------------------------------------------------------------------------


def test_record_call_increments_spend(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    acc = _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="x",
        cap=Decimal("100"), used=Decimal("10"),
    )
    record_call(session, account=acc, cost_usd=Decimal("5"))
    session.refresh(acc)
    assert acc.spending_used_this_month_usd == Decimal("15")
    assert acc.last_used_at is not None


def test_record_call_trips_cap_exhaustion(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """After enough record_calls, the resolver skips this account."""
    acc = _mk_account(
        session, business=business, unit_id=tree["romance"].id,
        label="r",
        cap=Decimal("10"), used=Decimal("0"),
    )
    _mk_account(
        session, business=business, label="company",
    )
    # Burn the cap
    for _ in range(11):
        record_call(session, account=acc, cost_usd=Decimal("1"))
    out = resolve_credentials(
        session,
        business_unit_id=tree["romance"].id,
        business_id=business.id,
        service=ExternalServiceKind.LLM_ANTHROPIC,
        deployment_mode=DeploymentMode.LOCAL,
    )
    # Resolver promoted to company default because the unit's account
    # is now over cap.
    assert out.source == "company_default"
