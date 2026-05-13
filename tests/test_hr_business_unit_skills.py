"""PR6 tests — HR skills for spawning + lifecycle BusinessUnit ops +
niche.score_fit skill.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.identity.model import Founder
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


def _ctx(session, business, founder) -> SkillContext:
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
    )


@pytest.fixture
def default_unit(
    session: Session, business: Business,
) -> BusinessUnit:
    return BusinessUnitBoard(session).create(
        business_id=business.id, name=business.name,
        kind=BusinessUnitKind.DEFAULT,
    )


# ---------------------------------------------------------------------------
# hr.start_business_line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_business_line_creates_line_under_default(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["hr.start_business_line"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"kind": "kdp"},
    )
    assert out.payload["kind"] == "kdp"
    assert out.payload["name"] == "KDP"
    new_id = out.payload["unit_id"]
    unit = BusinessUnitBoard(session).get(_uuid(new_id))
    assert unit is not None
    assert unit.kind == BusinessUnitKind.LINE
    assert unit.parent_id == default_unit.id


@pytest.mark.asyncio
async def test_start_business_line_supports_all_6_kinds(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["hr.start_business_line"]
    for kind in ["pod", "kdp", "info", "saas", "affiliate", "agency"]:
        out = await skill.run(
            ctx=_ctx(session, business, founder),
            args={"kind": kind},
        )
        assert out.payload["kind"] == kind


@pytest.mark.asyncio
async def test_start_business_line_rejects_unknown_kind(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["hr.start_business_line"]
    with pytest.raises(SkillError, match="kind must be one of"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"kind": "homesteading"},
        )


@pytest.mark.asyncio
async def test_start_business_line_accepts_custom_name(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["hr.start_business_line"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"kind": "kdp", "name": "KDP — Romance Pen"},
    )
    assert out.payload["name"] == "KDP — Romance Pen"


# ---------------------------------------------------------------------------
# Spawn type / audience / product VP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_type_manager_under_line(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.spawn_type_manager"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"parent_unit_id": str(line.id), "name": "Romance"},
    )
    new_id = out.payload["unit_id"]
    unit = board.get(_uuid(new_id))
    assert unit is not None
    assert unit.kind == BusinessUnitKind.TYPE
    assert unit.parent_id == line.id


@pytest.mark.asyncio
async def test_spawn_audience_manager_with_niche_profile(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="Affiliate",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.spawn_audience_manager"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "parent_unit_id": str(line.id),
            "name": "AI marketers",
            "niche_profile": {
                "core_topics": ["ai_marketing", "automation"],
                "off_limits_topics": ["homesteading"],
                "list_size": 12400,
            },
        },
    )
    unit = board.get(_uuid(out.payload["unit_id"]))
    assert unit is not None
    assert unit.kind == BusinessUnitKind.AUDIENCE
    assert unit.niche_profile is not None
    assert "ai_marketing" in unit.niche_profile["core_topics"]


@pytest.mark.asyncio
async def test_spawn_audience_manager_rejects_bad_profile(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="Affiliate",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.spawn_audience_manager"]
    with pytest.raises(SkillError, match="niche_profile"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "parent_unit_id": str(line.id),
                "name": "x",
                "niche_profile": {"core_topics": "not-a-list"},
            },
        )


@pytest.mark.asyncio
async def test_spawn_product_vp(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="SaaS",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.spawn_product_vp"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"parent_unit_id": str(line.id), "name": "Korpha"},
    )
    unit = board.get(_uuid(out.payload["unit_id"]))
    assert unit is not None
    assert unit.kind == BusinessUnitKind.PRODUCT_VP


@pytest.mark.asyncio
async def test_spawn_under_nonexistent_parent_raises(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["hr.spawn_type_manager"]
    with pytest.raises(SkillError, match="not found"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "parent_unit_id": str(uuid4()),
                "name": "Romance",
            },
        )


# ---------------------------------------------------------------------------
# pause / resume / archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_business_unit(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["hr.pause_business_unit"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "unit_id": str(default_unit.id),
            "reason": "founder break",
        },
    )
    assert out.payload["status"] == "paused"


@pytest.mark.asyncio
async def test_resume_business_unit(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    BusinessUnitBoard(session).pause(default_unit.id, reason="x")
    skill = default_registry.skills["hr.resume_business_unit"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"unit_id": str(default_unit.id)},
    )
    assert out.payload["status"] == "active"


@pytest.mark.asyncio
async def test_archive_leaf_unit(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    leaf = board.create(
        business_id=business.id, name="leaf",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.archive_business_unit"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"unit_id": str(leaf.id)},
    )
    assert out.payload["archived_count"] == 1


@pytest.mark.asyncio
async def test_archive_with_children_refused_without_cascade(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    board.create(
        business_id=business.id, name="Romance",
        kind=BusinessUnitKind.TYPE, parent_id=line.id,
    )
    skill = default_registry.skills["hr.archive_business_unit"]
    with pytest.raises(SkillError, match="live children"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"unit_id": str(line.id)},
        )


@pytest.mark.asyncio
async def test_archive_subtree_with_cascade(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    board.create(
        business_id=business.id, name="Romance",
        kind=BusinessUnitKind.TYPE, parent_id=line.id,
    )
    skill = default_registry.skills["hr.archive_business_unit"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"unit_id": str(line.id), "cascade": True},
    )
    assert out.payload["archived_count"] == 2


# ---------------------------------------------------------------------------
# niche.score_fit skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_fit_accepts_core_match(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    from korpha.business_units.model import NicheProfile
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="AI marketers",
        kind=BusinessUnitKind.AUDIENCE, parent_id=default_unit.id,
        niche_profile=NicheProfile(
            core_topics=["ai_marketing"],
        ),
    )
    skill = default_registry.skills["niche.score_fit"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "unit_id": str(unit.id),
            "work_topics": ["ai_marketing"],
        },
    )
    assert out.payload["verdict"] == "accept"


@pytest.mark.asyncio
async def test_score_fit_off_limits_declines(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    from korpha.business_units.model import NicheProfile
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="AI marketers",
        kind=BusinessUnitKind.AUDIENCE, parent_id=default_unit.id,
        niche_profile=NicheProfile(
            core_topics=["ai_marketing"],
            off_limits_topics=["homesteading"],
        ),
    )
    skill = default_registry.skills["niche.score_fit"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "unit_id": str(unit.id),
            "work_topics": ["homesteading"],
        },
    )
    assert out.payload["verdict"] == "decline"
    assert out.payload["off_limits_hit"] is True


@pytest.mark.asyncio
async def test_score_fit_no_profile_escalates(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    """Unit without niche_profile → defer to founder via escalate."""
    skill = default_registry.skills["niche.score_fit"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "unit_id": str(default_unit.id),
            "work_topics": ["anything"],
        },
    )
    assert out.payload["verdict"] == "escalate"
    assert "no niche_profile" in out.payload["reason"]


@pytest.mark.asyncio
async def test_score_fit_missing_unit_raises(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["niche.score_fit"]
    with pytest.raises(SkillError, match="not found"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "unit_id": str(uuid4()),
                "work_topics": ["x"],
            },
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _uuid(s):
    from uuid import UUID
    return UUID(str(s))
