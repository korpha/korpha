"""PR11 tests — LinePack contract + 6 reference packs.

Each pack:
- Has pack_id + line_kind matching a canonical line
- Ships a non-empty NicheProfile
- Defines ≥1 KPI
- Lists ≥1 suggested worker specialty
- Sets a customer-support autonomy level
- ``setup_unit`` applies the playbook to a fresh BusinessUnit
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, NicheProfile,
)
from korpha.line_packs import (
    AffiliateLinePack, AgencyLinePack, InfoLinePack,
    KdpLinePack, PodLinePack, SaasLinePack,
    default_registry,
)
from korpha.line_packs.contract import LinePack


ALL_PACKS = [
    PodLinePack, KdpLinePack, InfoLinePack,
    SaasLinePack, AffiliateLinePack, AgencyLinePack,
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_six_builtin_packs_registered() -> None:
    """All 6 canonical line packs auto-register at import time."""
    pack_ids = {p.pack_id for p in default_registry.all()}
    assert {
        "pod-line-pack@1.0.0",
        "kdp-line-pack@1.0.0",
        "info-line-pack@1.0.0",
        "saas-line-pack@1.0.0",
        "affiliate-line-pack@1.0.0",
        "agency-line-pack@1.0.0",
    } <= pack_ids


def test_registry_lookup_by_id() -> None:
    pack = default_registry.get("kdp-line-pack@1.0.0")
    assert pack is not None
    assert pack.line_kind == "kdp"


def test_registry_for_line_filters() -> None:
    pods = default_registry.for_line("pod")
    assert all(p.line_kind == "pod" for p in pods)
    assert len(pods) >= 1


# ---------------------------------------------------------------------------
# Per-pack sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack_cls", ALL_PACKS)
def test_pack_has_required_metadata(pack_cls) -> None:
    pack = pack_cls()
    assert pack.pack_id  # non-empty
    assert "@" in pack.pack_id  # versioned
    assert pack.line_kind in {
        "pod", "kdp", "info", "saas", "affiliate", "agency",
    }
    assert pack.description


@pytest.mark.parametrize("pack_cls", ALL_PACKS)
def test_pack_default_niche_profile_valid(pack_cls) -> None:
    pack = pack_cls()
    profile = pack.default_niche_profile()
    assert isinstance(profile, NicheProfile)
    # All canonical packs ship at least 1 core topic
    assert len(profile.core_topics) >= 1


@pytest.mark.parametrize("pack_cls", ALL_PACKS)
def test_pack_kpi_definitions_non_empty(pack_cls) -> None:
    pack = pack_cls()
    kpis = pack.kpi_definitions()
    assert len(kpis) >= 1
    for k in kpis:
        assert k.key and k.label and k.unit


@pytest.mark.parametrize("pack_cls", ALL_PACKS)
def test_pack_suggested_workers_non_empty(pack_cls) -> None:
    pack = pack_cls()
    workers = pack.suggested_worker_specialties()
    assert len(workers) >= 1


@pytest.mark.parametrize("pack_cls", ALL_PACKS)
def test_pack_autonomy_level_in_range(pack_cls) -> None:
    pack = pack_cls()
    level = pack.default_support_autonomy_level()
    assert 0 <= level <= 4


def test_kdp_autonomy_level_is_1() -> None:
    """Per PRODUCT_LIFECYCLE.md: KDP defaults Level 1-2 because
    reviews are reputation-critical. Pack picks Level 1 (draft for approval)."""
    assert KdpLinePack().default_support_autonomy_level() == 1


def test_saas_autonomy_level_is_3() -> None:
    """SaaS is the most mature support flow per docs — Level 3."""
    assert SaasLinePack().default_support_autonomy_level() == 3


# ---------------------------------------------------------------------------
# setup_unit
# ---------------------------------------------------------------------------


def test_setup_unit_applies_niche_profile(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE,
        parent_id=board.create(
            business_id=business.id, name="root",
            kind=BusinessUnitKind.DEFAULT,
        ).id,
    )
    pack = KdpLinePack()
    pack.setup_unit(session, unit)
    session.refresh(unit)
    assert unit.niche_profile is not None
    profile = NicheProfile.model_validate(unit.niche_profile)
    assert "kdp" in profile.core_topics


def test_setup_unit_writes_kpis_to_config(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    unit = board.create(
        business_id=business.id, name="SaaS",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    SaasLinePack().setup_unit(session, unit)
    session.refresh(unit)
    kpis = unit.config.get("kpis", [])
    keys = {k["key"] for k in kpis}
    assert "mrr_usd" in keys
    assert "churn_rate_monthly" in keys


def test_setup_unit_writes_autonomy_level(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    unit = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    KdpLinePack().setup_unit(session, unit)
    session.refresh(unit)
    assert unit.config["support_autonomy_level"] == 1


def test_setup_unit_writes_pack_id_to_unit(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    unit = board.create(
        business_id=business.id, name="POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    PodLinePack().setup_unit(session, unit)
    session.refresh(unit)
    assert unit.playbook_skill_pack == "pod-line-pack@1.0.0"


def test_setup_unit_writes_required_services(
    session: Session, business: Business,
) -> None:
    """Setup wizard reads required_services to prompt for credentials."""
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    unit = board.create(
        business_id=business.id, name="Affiliate",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    AffiliateLinePack().setup_unit(session, unit)
    session.refresh(unit)
    services = unit.config.get("required_services", [])
    assert "jvzoo" in services
    assert "convertkit" in services
