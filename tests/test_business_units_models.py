"""PR1 tests — BusinessUnit + Product models + Board CRUD + tree walks.

Covers:
- Create + slug normalization + sibling uniqueness
- Parent rules (DEFAULT root vs non-DEFAULT needing parent)
- Cross-business reparenting refused
- memory_namespace_id is auto + unique + immutable
- Ancestors / descendants / subtree / iter_walk_up tree operations
- Niche profile validation roundtrip
- Pause / resume / archive lifecycle
- Archive guard (refuses with live children) + archive_subtree cascade
- Product CRUD with slug uniqueness + time-bound validation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.business_units.board import (
    BusinessUnitBoard, BusinessUnitError, slugify,
)
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, DeploymentMode,
    NicheProfile, Product, ProductKind,
)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_basic() -> None:
    assert slugify("KDP — Romance") == "kdp-romance"


def test_slugify_collapses_dashes() -> None:
    assert slugify("a---b___c") == "a-b-c"


def test_slugify_trims_dashes() -> None:
    assert slugify("---x---") == "x"


def test_slugify_empty_falls_back() -> None:
    assert slugify("") == "unit"
    assert slugify("!@#$") == "unit"
    assert slugify("   ") == "unit"


def test_slugify_caps_length() -> None:
    assert len(slugify("a" * 200)) <= 60


# ---------------------------------------------------------------------------
# Enums sanity
# ---------------------------------------------------------------------------


def test_deployment_mode_values() -> None:
    assert DeploymentMode.LOCAL == "local"
    assert DeploymentMode.SAAS == "saas"


def test_business_unit_kind_includes_six_canonical_lines() -> None:
    """All 6 canonical line kinds + DEFAULT/CUSTOM must be enumerable."""
    values = {k.value for k in BusinessUnitKind}
    assert {
        "default", "line", "type", "series",
        "niche", "audience", "product_vp", "custom",
    } <= values


def test_product_kind_covers_all_line_outputs() -> None:
    """Each canonical line ships at least one ProductKind it produces."""
    values = {k.value for k in ProductKind}
    # POD = design; KDP = book; Info = course/ebook/newsletter/membership;
    # SaaS = saas_app; Affiliate = campaign; Agency = service.
    assert {
        "book", "design", "course", "ebook", "newsletter",
        "membership", "saas_app", "campaign", "service", "custom",
    } <= values


# ---------------------------------------------------------------------------
# Create + slug uniqueness
# ---------------------------------------------------------------------------


def test_create_default_root_succeeds(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id,
        name="Marketro LLC",
        kind=BusinessUnitKind.DEFAULT,
        parent_id=None,
    )
    assert unit.parent_id is None
    assert unit.kind == BusinessUnitKind.DEFAULT
    assert unit.slug == "marketro-llc"
    assert unit.status == "active"
    assert unit.memory_namespace_id is not None


def test_create_line_under_default_succeeds(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id,
        name="Marketro LLC",
        kind=BusinessUnitKind.DEFAULT,
    )
    line = board.create(
        business_id=business.id,
        name="KDP",
        kind=BusinessUnitKind.LINE,
        parent_id=root.id,
    )
    assert line.parent_id == root.id
    assert line.slug == "kdp"


def test_create_non_default_without_parent_refused(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    with pytest.raises(BusinessUnitError, match="requires a parent_id"):
        board.create(
            business_id=business.id,
            name="orphan line",
            kind=BusinessUnitKind.LINE,
            parent_id=None,
        )


def test_create_default_with_parent_refused(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id,
        name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    with pytest.raises(BusinessUnitError, match="DEFAULT unit cannot"):
        board.create(
            business_id=business.id,
            name="second root",
            kind=BusinessUnitKind.DEFAULT,
            parent_id=root.id,
        )


def test_create_with_empty_name_refused(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    with pytest.raises(BusinessUnitError, match="name required"):
        board.create(
            business_id=business.id,
            name="   ",
            kind=BusinessUnitKind.DEFAULT,
        )


def test_sibling_slug_collision_refused(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    with pytest.raises(BusinessUnitError, match="sibling slug"):
        board.create(
            business_id=business.id, name="KDP",
            kind=BusinessUnitKind.LINE, parent_id=root.id,
        )


def test_cross_branch_slug_collision_allowed(
    session: Session, business: Business,
) -> None:
    """Same slug under different parents is fine."""
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    pod = board.create(
        business_id=business.id, name="POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    kdp = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    pod_niche = board.create(
        business_id=business.id, name="Cat Lovers",
        kind=BusinessUnitKind.NICHE, parent_id=pod.id,
    )
    kdp_niche = board.create(
        business_id=business.id, name="Cat Lovers",
        kind=BusinessUnitKind.NICHE, parent_id=kdp.id,
    )
    assert pod_niche.slug == "cat-lovers"
    assert kdp_niche.slug == "cat-lovers"
    assert pod_niche.parent_id != kdp_niche.parent_id


def test_parent_must_exist(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    with pytest.raises(BusinessUnitError, match="parent unit .* not found"):
        board.create(
            business_id=business.id, name="x",
            kind=BusinessUnitKind.LINE, parent_id=uuid4(),
        )


def test_parent_must_belong_to_same_business(
    session: Session, business: Business, founder,
) -> None:
    """Cannot create a child under a parent in a different business."""
    other_biz = Business(
        founder_id=founder.id, name="OtherCo",
        description="x", founder_brief={},
    )
    session.add(other_biz)
    session.commit()
    session.refresh(other_biz)
    board = BusinessUnitBoard(session)
    other_root = board.create(
        business_id=other_biz.id, name="OtherCo root",
        kind=BusinessUnitKind.DEFAULT,
    )
    with pytest.raises(BusinessUnitError, match="different business"):
        board.create(
            business_id=business.id, name="hijack",
            kind=BusinessUnitKind.LINE, parent_id=other_root.id,
        )


def test_create_under_archived_parent_refused(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    board.archive(root.id)
    with pytest.raises(BusinessUnitError, match="archived parent"):
        board.create(
            business_id=business.id, name="child",
            kind=BusinessUnitKind.LINE, parent_id=root.id,
        )


# ---------------------------------------------------------------------------
# memory_namespace_id
# ---------------------------------------------------------------------------


def test_each_unit_gets_unique_namespace(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    u1 = board.create(
        business_id=business.id, name="line1",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    u2 = board.create(
        business_id=business.id, name="line2",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    namespaces = {root.memory_namespace_id, u1.memory_namespace_id, u2.memory_namespace_id}
    assert len(namespaces) == 3
    assert all(ns is not None for ns in namespaces)


# ---------------------------------------------------------------------------
# Niche profile validation
# ---------------------------------------------------------------------------


def test_niche_profile_roundtrip(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    profile = NicheProfile(
        core_topics=["ai_marketing", "automation"],
        adjacent_topics=["copywriting", "analytics"],
        off_limits_topics=["homesteading"],
        persona="marketing managers at 5-50 person SaaS",
        list_size=12400,
        avg_open_rate=0.31,
        avg_click_rate=0.04,
        avg_epc=1.85,
        last_burned_at=None,
        promos_in_last_30_days=2,
    )
    unit = board.create(
        business_id=business.id, name="AI marketers",
        kind=BusinessUnitKind.AUDIENCE,
        parent_id=root.id,
        niche_profile=profile,
    )
    assert unit.niche_profile is not None
    reloaded = NicheProfile.model_validate(unit.niche_profile)
    assert reloaded.core_topics == ["ai_marketing", "automation"]
    assert reloaded.list_size == 12400
    assert reloaded.last_burn_unsubscribes == 0   # default
    assert reloaded.last_promoted_at is None      # default


def test_niche_profile_update_validates(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="root",
        kind=BusinessUnitKind.DEFAULT,
    )
    profile = NicheProfile(core_topics=["x"])
    unit = board.create(
        business_id=business.id, name="line",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
        niche_profile=profile,
    )
    updated = NicheProfile(
        core_topics=["x", "y"],
        promos_in_last_30_days=5,
    )
    out = board.update_niche_profile(unit.id, updated)
    assert out.niche_profile is not None
    reloaded = NicheProfile.model_validate(out.niche_profile)
    assert reloaded.core_topics == ["x", "y"]
    assert reloaded.promos_in_last_30_days == 5


# ---------------------------------------------------------------------------
# Tree operations
# ---------------------------------------------------------------------------


@pytest.fixture
def marketro_tree(
    session: Session, business: Business,
) -> dict[str, BusinessUnit]:
    """Build a realistic 4-level tree mirroring the Marketro walkthrough.

    Returns a dict keyed by slug for tests to navigate.
    """
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro LLC",
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
    highland = board.create(
        business_id=business.id, name="Highland Rogue Saga",
        kind=BusinessUnitKind.SERIES, parent_id=romance.id,
    )
    pod = board.create(
        business_id=business.id, name="POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    tshirts = board.create(
        business_id=business.id, name="T-Shirts",
        kind=BusinessUnitKind.TYPE, parent_id=pod.id,
    )
    return {
        "root": root, "kdp": kdp, "romance": romance,
        "highland": highland, "pod": pod, "tshirts": tshirts,
    }


def test_ancestors_walks_up(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    chain = board.ancestors(marketro_tree["highland"].id)
    slugs = [u.slug for u in chain]
    # Highland → Romance → KDP → Marketro (root); excludes self
    assert slugs == ["romance", "kdp", "marketro-llc"]


def test_ancestors_of_root_is_empty(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    assert board.ancestors(marketro_tree["root"].id) == []


def test_descendants_returns_subtree_bfs(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    descendants = board.descendants(marketro_tree["root"].id)
    slugs = {u.slug for u in descendants}
    # Everything except root
    assert slugs == {"kdp", "romance", "highland-rogue-saga", "pod", "t-shirts"}


def test_subtree_includes_self(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    nodes = board.subtree(marketro_tree["kdp"].id)
    slugs = [u.slug for u in nodes]
    assert slugs[0] == "kdp"  # BFS root first
    assert set(slugs) == {"kdp", "romance", "highland-rogue-saga"}


def test_iter_walk_up_starts_with_self(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    """Used by the credentials resolver (PR4) to walk leaf → root.
    Self comes first; generator stops at root."""
    board = BusinessUnitBoard(session)
    chain = list(board.iter_walk_up(marketro_tree["highland"].id))
    slugs = [u.slug for u in chain]
    assert slugs == [
        "highland-rogue-saga", "romance", "kdp", "marketro-llc",
    ]


def test_children_direct_only(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    direct = board.children(marketro_tree["root"].id)
    # Root has 2 direct kids: KDP and POD. Not descendants.
    slugs = {u.slug for u in direct}
    assert slugs == {"kdp", "pod"}


def test_list_for_business_returns_all_active(
    session: Session, business: Business,
    marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    all_units = board.list_for_business(business.id)
    assert len(all_units) == 6   # root + kdp + romance + highland + pod + tshirts


def test_list_for_business_filters_archived_by_default(
    session: Session, business: Business,
    marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    board.archive(marketro_tree["tshirts"].id)
    active = board.list_for_business(business.id)
    assert len(active) == 5
    everything = board.list_for_business(
        business.id, include_archived=True,
    )
    assert len(everything) == 6


# ---------------------------------------------------------------------------
# Lifecycle: pause / resume / archive
# ---------------------------------------------------------------------------


def test_pause_then_resume(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    paused = board.pause(
        marketro_tree["kdp"].id, reason="founder wants break",
    )
    assert paused.status == "paused"
    assert paused.paused_reason == "founder wants break"
    assert paused.paused_at is not None

    resumed = board.resume(marketro_tree["kdp"].id)
    assert resumed.status == "active"
    assert resumed.paused_reason is None
    assert resumed.paused_at is None


def test_archive_blocks_when_live_children(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    with pytest.raises(BusinessUnitError, match="live children"):
        board.archive(marketro_tree["kdp"].id)


def test_archive_leaf_succeeds(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    archived = board.archive(marketro_tree["tshirts"].id)
    assert archived.status == "archived"


def test_archive_subtree_cascades_leaves_first(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    archived_units = board.archive_subtree(marketro_tree["kdp"].id)
    statuses = {u.slug: u.status for u in archived_units}
    assert statuses["kdp"] == "archived"
    assert statuses["romance"] == "archived"
    assert statuses["highland-rogue-saga"] == "archived"


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def test_add_product_under_unit(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    book = board.add_product(
        business_unit_id=marketro_tree["highland"].id,
        name="Highland Rogue",
        kind=ProductKind.BOOK,
        attributes={"asin": "B0CXXXX", "kindle_unlimited_pages": 312},
    )
    assert book.business_unit_id == marketro_tree["highland"].id
    # Denormalized business_id matches the unit's
    assert book.business_id == marketro_tree["highland"].business_id
    assert book.slug == "highland-rogue"
    assert book.attributes["asin"] == "B0CXXXX"


def test_product_slug_collision_within_unit_refused(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    board.add_product(
        business_unit_id=marketro_tree["highland"].id,
        name="Highland Rogue",
        kind=ProductKind.BOOK,
    )
    with pytest.raises(BusinessUnitError, match="product slug"):
        board.add_product(
            business_unit_id=marketro_tree["highland"].id,
            name="Highland Rogue",
            kind=ProductKind.BOOK,
        )


def test_product_slug_collision_across_units_allowed(
    session: Session, business: Business,
    marketro_tree: dict[str, BusinessUnit],
) -> None:
    """Same product slug under different units is OK."""
    board = BusinessUnitBoard(session)
    board.add_product(
        business_unit_id=marketro_tree["highland"].id,
        name="Volume 1", kind=ProductKind.BOOK,
    )
    # Different series can also have a "Volume 1" book.
    other_series = board.create(
        business_id=business.id, name="Highland Curse Saga",
        kind=BusinessUnitKind.SERIES,
        parent_id=marketro_tree["romance"].id,
    )
    board.add_product(
        business_unit_id=other_series.id,
        name="Volume 1", kind=ProductKind.BOOK,
    )  # no error


def test_product_time_bound_validation(
    session: Session, business: Business,
    marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    affiliate_line = board.create(
        business_id=business.id, name="Affiliate",
        kind=BusinessUnitKind.LINE,
        parent_id=marketro_tree["root"].id,
    )
    audience = board.create(
        business_id=business.id, name="AI marketers",
        kind=BusinessUnitKind.AUDIENCE,
        parent_id=affiliate_line.id,
    )
    start = datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)
    end_before_start = start - timedelta(hours=1)
    with pytest.raises(BusinessUnitError, match="ends_at must be"):
        board.add_product(
            business_unit_id=audience.id,
            name="Promote Korpha",
            kind=ProductKind.CAMPAIGN,
            starts_at=start,
            ends_at=end_before_start,
        )
    # Valid range succeeds
    campaign = board.add_product(
        business_unit_id=audience.id,
        name="Promote Korpha",
        kind=ProductKind.CAMPAIGN,
        starts_at=start,
        ends_at=start + timedelta(days=4),
    )
    assert campaign.ends_at is not None


def test_add_product_to_archived_unit_refused(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    board.archive(marketro_tree["tshirts"].id)
    with pytest.raises(BusinessUnitError, match="archived unit"):
        board.add_product(
            business_unit_id=marketro_tree["tshirts"].id,
            name="Cat Lovers Tee",
            kind=ProductKind.DESIGN,
        )


def test_list_products_filters_archived(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    board = BusinessUnitBoard(session)
    book = board.add_product(
        business_unit_id=marketro_tree["highland"].id,
        name="Highland Rogue", kind=ProductKind.BOOK,
    )
    # Archive product directly
    book.status = "archived"
    session.add(book)
    session.commit()
    active = board.list_products(marketro_tree["highland"].id)
    assert active == []
    all_products = board.list_products(
        marketro_tree["highland"].id, include_archived=True,
    )
    assert len(all_products) == 1


# ---------------------------------------------------------------------------
# Resolver-style walks (PR4 will consume these)
# ---------------------------------------------------------------------------


def test_iter_walk_up_terminates_on_root(
    session: Session, marketro_tree: dict[str, BusinessUnit],
) -> None:
    """Walking from root yields self only, then stops."""
    board = BusinessUnitBoard(session)
    chain = list(board.iter_walk_up(marketro_tree["root"].id))
    assert len(chain) == 1
    assert chain[0].id == marketro_tree["root"].id


def test_iter_walk_up_orphan_unit_yields_self(
    session: Session, business: Business,
) -> None:
    """A standalone DEFAULT unit (root) yields only itself."""
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="solo",
        kind=BusinessUnitKind.DEFAULT,
    )
    chain = list(board.iter_walk_up(root.id))
    assert [u.id for u in chain] == [root.id]
