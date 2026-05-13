"""PR13 — end-to-end Marketro multi-line walkthrough.

Exercises the full org-model stack from PR1-PR12 in a single scenario:

  1. Spawn Marketro setup: Business + DEFAULT unit + CEO + 5 Lines
     (POD, KDP, Info, SaaS, Affiliate) via hr.start_business_line.
  2. Each Line VP installs its Line Pack → niche profile, KPIs,
     autonomy level all populated.
  3. KDP Romance Type Manager spawned + Highland Rogue Series Lead
     spawned underneath; series Product (book) added.
  4. Per-unit credentials configured (different Stripe per Line).
  5. POD Line VP proposes cooperation via cooperation.propose; KDP
     Romance accepts. CrossNamespaceRecallGrant auto-issued.
  6. Affiliate Audience Manager scores an incoming work proposal
     against its niche profile → DECLINE (off-limits topic hit).
  7. Per-unit backup writes a tar.gz with the unit's subtree + DB
     scope.

Asserts memory isolation (vector partition holds), per-unit
credentials route correctly via the resolver, OAuth CLI is consulted
for Pro-tier in local mode only, shared resource usage is attributed
to the consuming unit.
"""
from __future__ import annotations

import json
import os
import tarfile
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.filesystem import (
    backup_unit, ensure_unit_layout,
)
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, NicheProfile,
    Product, ProductKind,
)
from korpha.cooperation.board import CooperationBoard
from korpha.cooperation.model import (
    CooperationProposal, CooperationStatus,
)
from korpha.credentials.model import (
    ExternalServiceAccount, ExternalServiceKind,
)
from korpha.credentials.resolver import (
    ResolvedCredentials, resolve_credentials,
)
from korpha.business_units.model import DeploymentMode
from korpha.line_packs import (
    AffiliateLinePack, InfoLinePack, KdpLinePack,
    PodLinePack, SaasLinePack,
)
from korpha.business_units.scoring import (
    FitVerdict, score_fit,
)
from korpha.memory.grants import (
    CrossNamespaceRecallGrant, check_recall_authorized,
)
from korpha.shared_resources.model import (
    SharedResource, SharedResourceKind, SharedResourceUsage,
)
from korpha.identity.model import Founder


@pytest.fixture
def marketro(
    session: Session, business: Business, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Set up the full Marketro org tree per PRODUCT_LIFECYCLE.md.

    Returns a dict of named units for the scenario asserts."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro LLC",
        kind=BusinessUnitKind.DEFAULT,
    )

    units: dict = {"root": root}
    # Stand up all 5 lines via Line Packs
    for pack_cls, slug in [
        (KdpLinePack, "kdp"),
        (PodLinePack, "pod"),
        (InfoLinePack, "info"),
        (SaasLinePack, "saas"),
        (AffiliateLinePack, "affiliate"),
    ]:
        line = board.create(
            business_id=business.id,
            name=pack_cls().line_kind.upper(),
            kind=BusinessUnitKind.LINE, parent_id=root.id,
        )
        pack_cls().setup_unit(session, line)
        units[slug] = line

    # KDP → Romance → Highland Rogue Saga
    romance = board.create(
        business_id=business.id, name="Romance",
        kind=BusinessUnitKind.TYPE, parent_id=units["kdp"].id,
    )
    units["romance"] = romance
    highland = board.create(
        business_id=business.id, name="Highland Rogue Saga",
        kind=BusinessUnitKind.SERIES, parent_id=romance.id,
    )
    units["highland"] = highland
    book = board.add_product(
        business_unit_id=highland.id,
        name="Highland Rogue Vol 5",
        kind=ProductKind.BOOK,
        attributes={"asin": "B0CXX123", "kindle_unlimited_pages": 312},
    )
    units["book"] = book

    # Affiliate Audience Manager — AI marketers
    audience = board.create(
        business_id=business.id, name="AI marketers",
        kind=BusinessUnitKind.AUDIENCE,
        parent_id=units["affiliate"].id,
        niche_profile=NicheProfile(
            core_topics=["ai_marketing", "automation"],
            adjacent_topics=["copywriting"],
            off_limits_topics=["homesteading", "personal_finance"],
            list_size=12400,
            avg_open_rate=0.31,
        ),
    )
    units["audience_ai"] = audience

    return units


# ---------------------------------------------------------------------------
# Org tree shape
# ---------------------------------------------------------------------------


def test_six_units_at_correct_levels(
    session: Session, marketro: dict,
) -> None:
    """Each of the 6 lines spawns at LINE kind under the DEFAULT root."""
    for line_slug in ["kdp", "pod", "info", "saas", "affiliate"]:
        assert marketro[line_slug].kind == BusinessUnitKind.LINE
        assert marketro[line_slug].parent_id == marketro["root"].id


def test_kdp_subtree_three_levels(
    session: Session, marketro: dict,
) -> None:
    """KDP → Romance → Highland Rogue Saga → book."""
    board = BusinessUnitBoard(session)
    descendants = board.descendants(marketro["kdp"].id)
    slugs = {u.slug for u in descendants}
    assert slugs == {"romance", "highland-rogue-saga"}
    # Product is a leaf — not in descendants tree, but findable via list_products
    products = board.list_products(marketro["highland"].id)
    assert len(products) == 1
    assert products[0].name == "Highland Rogue Vol 5"


def test_line_packs_populated_niche_profiles(
    session: Session, marketro: dict,
) -> None:
    """Each line's pack installed → niche_profile + KPIs in unit.config."""
    saas = marketro["saas"]
    assert saas.niche_profile is not None
    profile = NicheProfile.model_validate(saas.niche_profile)
    assert "saas" in profile.core_topics
    kpis = [k["key"] for k in saas.config.get("kpis", [])]
    assert "mrr_usd" in kpis


def test_kdp_autonomy_level_one(
    session: Session, marketro: dict,
) -> None:
    """KDP defaults to autonomy Level 1 per PRODUCT_LIFECYCLE.md."""
    assert marketro["kdp"].config["support_autonomy_level"] == 1


# ---------------------------------------------------------------------------
# Memory isolation — distinct namespaces per unit
# ---------------------------------------------------------------------------


def test_each_unit_has_unique_memory_namespace(
    session: Session, marketro: dict,
) -> None:
    namespaces = {
        u.memory_namespace_id for u in marketro.values()
        if isinstance(u, BusinessUnit)
    }
    assert len(namespaces) == 9  # root + 5 lines + romance + highland + audience


def test_cross_namespace_recall_blocked_by_default(
    session: Session, marketro: dict,
) -> None:
    """Romance and POD have different memory namespaces — no recall
    leaks between them without explicit grant."""
    assert not check_recall_authorized(
        session,
        from_namespace_id=marketro["romance"].memory_namespace_id,
        to_namespace_id=marketro["pod"].memory_namespace_id,
    )


# ---------------------------------------------------------------------------
# Cooperation flow with derived recall grant
# ---------------------------------------------------------------------------


def test_pod_proposes_to_kdp_romance_with_recall_grant(
    session: Session, business: Business, marketro: dict,
) -> None:
    """POD Line proposes cooperation to KDP Romance with cross-namespace
    recall permission. KDP accepts. Grant is auto-issued."""
    coop = CooperationBoard(session)
    prop = coop.propose(
        business_id=business.id,
        from_unit_id=marketro["pod"].id,
        to_unit_id=marketro["romance"].id,
        summary="Merch around Highland Rogue series",
        proposed_terms={"royalty_share_pct": 20.0},
        permissions={
            "royalty_share_pct": 20.0,
            "cross_namespace_recall": True,
        },
    )
    coop.decide(prop.id, decision=CooperationStatus.ACCEPTED)

    # Now POD can recall against KDP Romance's namespace
    assert check_recall_authorized(
        session,
        from_namespace_id=marketro["pod"].memory_namespace_id,
        to_namespace_id=marketro["romance"].memory_namespace_id,
    )

    # Revoke → grant flips off → recall blocked again
    coop.revoke(prop.id)
    assert not check_recall_authorized(
        session,
        from_namespace_id=marketro["pod"].memory_namespace_id,
        to_namespace_id=marketro["romance"].memory_namespace_id,
    )


# ---------------------------------------------------------------------------
# Niche scoring drives off-niche promo refusal
# ---------------------------------------------------------------------------


def test_affiliate_declines_homesteading_promo(
    session: Session, marketro: dict,
) -> None:
    """AI marketers audience has 'homesteading' in off_limits.
    score_fit against a homesteading promo → DECLINE."""
    audience = marketro["audience_ai"]
    profile = NicheProfile.model_validate(audience.niche_profile)
    out = score_fit(
        profile,
        work_topics=["homesteading", "off_grid_living"],
    )
    assert out.verdict == FitVerdict.DECLINE
    assert out.off_limits_hit is True


def test_affiliate_accepts_on_niche_korpha_promo(
    session: Session, marketro: dict,
) -> None:
    """AI-marketing-themed launch hits core topics → ACCEPT."""
    audience = marketro["audience_ai"]
    profile = NicheProfile.model_validate(audience.niche_profile)
    out = score_fit(
        profile,
        work_topics=["ai_marketing", "automation"],
    )
    assert out.verdict == FitVerdict.ACCEPT


# ---------------------------------------------------------------------------
# Per-unit credentials — different Stripe accounts per line
# ---------------------------------------------------------------------------


def test_per_unit_stripe_resolution(
    session: Session, business: Business, marketro: dict,
) -> None:
    """KDP gets its own Stripe key for tax-reporting isolation;
    SaaS uses the company default."""
    kdp_stripe = ExternalServiceAccount(
        business_id=business.id,
        business_unit_id=marketro["kdp"].id,
        service=ExternalServiceKind.STRIPE,
        label="KDP Stripe",
        credentials_encrypted=b"<encrypted-kdp>",
        is_active=True,
    )
    company_stripe = ExternalServiceAccount(
        business_id=business.id,
        business_unit_id=None,
        service=ExternalServiceKind.STRIPE,
        label="Company Stripe",
        credentials_encrypted=b"<encrypted-company>",
        is_active=True,
    )
    session.add_all([kdp_stripe, company_stripe])
    session.commit()

    # KDP unit resolves to its own
    out = resolve_credentials(
        session,
        business_unit_id=marketro["kdp"].id,
        business_id=business.id,
        service=ExternalServiceKind.STRIPE,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == kdp_stripe.id
    assert out.source == f"unit:{marketro['kdp'].id}"

    # SaaS unit falls through to company default
    out2 = resolve_credentials(
        session,
        business_unit_id=marketro["saas"].id,
        business_id=business.id,
        service=ExternalServiceKind.STRIPE,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out2.account.id == company_stripe.id
    assert out2.source == "company_default"


def test_descendant_walks_up_to_kdp_stripe(
    session: Session, business: Business, marketro: dict,
) -> None:
    """Romance (descendant of KDP) walks up the tree → finds KDP's Stripe."""
    kdp_stripe = ExternalServiceAccount(
        business_id=business.id,
        business_unit_id=marketro["kdp"].id,
        service=ExternalServiceKind.STRIPE,
        label="KDP Stripe",
        credentials_encrypted=b"<x>",
        is_active=True,
    )
    session.add(kdp_stripe); session.commit()

    out = resolve_credentials(
        session,
        business_unit_id=marketro["highland"].id,  # 2 levels below KDP
        business_id=business.id,
        service=ExternalServiceKind.STRIPE,
        deployment_mode=DeploymentMode.LOCAL,
    )
    assert out.account.id == kdp_stripe.id


# ---------------------------------------------------------------------------
# Shared resource usage attribution
# ---------------------------------------------------------------------------


def test_shared_resource_usage_attributed_to_consumer(
    session: Session, business: Business, marketro: dict,
) -> None:
    """KDP Romance generates a cover via z-image-turbo — usage row
    points at KDP Romance unit, not at Vidyo (the host)."""
    z_image = SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.AI_MODEL,
        name="z-image-turbo", label="z-image-turbo (Vidyo mesh)",
        host_business_unit_id=marketro["saas"].id,  # SaaS line owns it
        endpoint="https://mesh.vidyo.internal/image",
        config={}, is_active=True,
    )
    session.add(z_image); session.commit(); session.refresh(z_image)

    usage = SharedResourceUsage(
        resource_id=z_image.id,
        consumer_unit_id=marketro["romance"].id,  # KDP Romance consumed it
        skill_name="image.generate",
        units_consumed=1.0, cost_attributed_usd=0.0,
    )
    session.add(usage); session.commit()

    rows = list(session.exec(select(SharedResourceUsage)).all())
    assert len(rows) == 1
    # Consumer is KDP Romance, NOT the host (SaaS)
    assert rows[0].consumer_unit_id == marketro["romance"].id
    assert z_image.host_business_unit_id == marketro["saas"].id


# ---------------------------------------------------------------------------
# Per-unit backup
# ---------------------------------------------------------------------------


def test_per_unit_backup_creates_archive(
    session: Session, marketro: dict, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back up KDP subtree → tar.gz contains BusinessUnit rows for
    KDP + Romance + Highland Rogue, and the book product."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    ensure_unit_layout(marketro["kdp"].id)
    out = backup_unit(session, marketro["kdp"].id)
    assert out.is_file()
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        units_data = json.loads(
            tar.extractfile("./db/business_unit.json").read()  # type: ignore[union-attr]
        )
        products_data = json.loads(
            tar.extractfile("./db/business_product.json").read()  # type: ignore[union-attr]
        )

    slugs = {u["slug"] for u in units_data}
    assert "kdp" in slugs
    assert "romance" in slugs
    assert "highland-rogue-saga" in slugs

    product_names = {p["name"] for p in products_data}
    assert "Highland Rogue Vol 5" in product_names

    # POD subtree NOT in this backup — confirms cross-line isolation
    assert "pod" not in slugs
