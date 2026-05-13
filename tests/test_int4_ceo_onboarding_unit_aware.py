"""PR-INT-4 tests — CEO + onboarding chain awareness of BusinessUnits."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.context import (
    CANONICAL_LINE_KINDS,
    list_units_for_context,
    render_unit_summary_for_prompt,
)
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.identity.model import Founder


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def test_canonical_line_kinds_covers_all_6_plus_default() -> None:
    """Onboarding form picker offers default + 6 canonical lines."""
    values = {opt["value"] for opt in CANONICAL_LINE_KINDS}
    assert {"default", "pod", "kdp", "info", "saas", "affiliate", "agency"} <= values


def test_render_unit_summary_empty_business_gives_starter_hint(
    session: Session, business: Business,
) -> None:
    """No units → CEO sees a hint about calling hr.start_business_line."""
    summary = render_unit_summary_for_prompt(session, business.id)
    assert "No business units" in summary
    assert "hr.start_business_line" in summary


def test_render_unit_summary_lists_tree(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    summary = render_unit_summary_for_prompt(session, business.id)
    assert "Marketro" in summary
    assert "KDP" in summary
    assert "line" in summary
    assert "niche.score_fit" in summary  # surfaces available skills


def test_list_units_returns_summary_dicts(
    session: Session, business: Business,
) -> None:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    units = list_units_for_context(session, business.id)
    assert len(units) == 1
    assert units[0].name == "Marketro"
    assert units[0].kind == "default"


# ---------------------------------------------------------------------------
# Onboarding chain spawns Line when line_kind passed
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path):
    """Disk-backed sqlite so cross-Session writes (chain uses its own
    Session) persist correctly."""
    db_path = tmp_path / "chain.db"
    import korpha.db.registry  # noqa: F401
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_business(db_engine) -> tuple[Business, Founder]:
    with Session(db_engine) as session:
        f = Founder(email="a@b.com", display_name="A")
        session.add(f); session.commit(); session.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="x", founder_brief={},
        )
        session.add(b); session.commit(); session.refresh(b)
        return b, f


@pytest.mark.asyncio
async def test_chain_spawns_line_when_line_kind_kdp(
    db_engine,
) -> None:
    """line_kind=kdp passes through to hr.start_business_line."""
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    from korpha.onboarding.chain import run_post_pick_niche_chain

    b, _ = _seed_business(db_engine)

    def factory() -> CostTracker:
        return CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        ))

    await run_post_pick_niche_chain(
        engine=db_engine,
        business_id=b.id,
        niche={"name": "Highland romance", "value_prop": "x", "target_avatar": "y"},
        cost_tracker_factory=factory,
        line_kind="kdp",
    )
    # A KDP LINE unit must exist
    with Session(db_engine) as session:
        units = list(session.exec(select(BusinessUnit)).all())
        kinds = {u.kind.value for u in units}
        assert "default" in kinds   # auto-created
        assert "line" in kinds      # KDP spawned
        kdp_unit = next(u for u in units if u.kind.value == "line")
        # Line Pack applied
        assert kdp_unit.playbook_skill_pack == "kdp-line-pack@1.0.0"
        assert kdp_unit.niche_profile is not None


@pytest.mark.asyncio
async def test_chain_without_line_kind_back_compat(
    db_engine,
) -> None:
    """No line_kind → no line spawn (legacy single-CEO behavior)."""
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    from korpha.onboarding.chain import run_post_pick_niche_chain

    b, _ = _seed_business(db_engine)

    def factory() -> CostTracker:
        return CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        ))

    await run_post_pick_niche_chain(
        engine=db_engine,
        business_id=b.id,
        niche={"name": "x", "value_prop": "y", "target_avatar": "z"},
        cost_tracker_factory=factory,
    )
    with Session(db_engine) as session:
        units = list(session.exec(select(BusinessUnit)).all())
    # No units created (back-compat with pre-PR1 onboarding)
    assert units == []


@pytest.mark.asyncio
async def test_chain_default_line_kind_skips_spawn(
    db_engine,
) -> None:
    """line_kind='default' is back-compat alias → no spawn."""
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    from korpha.onboarding.chain import run_post_pick_niche_chain

    b, _ = _seed_business(db_engine)

    def factory() -> CostTracker:
        return CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        ))

    await run_post_pick_niche_chain(
        engine=db_engine,
        business_id=b.id,
        niche={"name": "x", "value_prop": "y", "target_avatar": "z"},
        cost_tracker_factory=factory,
        line_kind="default",
    )
    with Session(db_engine) as session:
        units = list(session.exec(select(BusinessUnit)).all())
    assert units == []
