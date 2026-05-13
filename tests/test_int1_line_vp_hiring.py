"""PR-INT-1 tests — owner agent hire on unit-spawn + SkillContext unit field.

Verifies the hire flow without exercising the workforce dispatcher
(that's exercised in the e2e walkthrough). Workforce code path read +
threaded the field; this PR adds the field, the hire, and the wiring
of owner_agent_role_id back onto the unit.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, NicheProfile,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.skills import default_registry
from korpha.skills.types import SkillContext


def _ctx(session, business, founder) -> SkillContext:
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


def test_skill_context_carries_business_unit_id() -> None:
    """The SkillContext dataclass exposes a business_unit_id field
    (default None) so downstream skills can scope by unit."""
    from korpha.skills.types import SkillContext as SC
    assert "business_unit_id" in SC.__dataclass_fields__


def test_skill_context_business_unit_id_defaults_to_none(
    session: Session, business: Business, founder: Founder,
) -> None:
    ctx = _ctx(session, business, founder)
    assert ctx.business_unit_id is None


def test_skill_context_accepts_business_unit_id(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
        business_unit_id=default_unit.id,
    )
    assert ctx.business_unit_id == default_unit.id


# ---------------------------------------------------------------------------
# HiringService accepts business_unit_id
# ---------------------------------------------------------------------------


def test_hiring_service_accepts_business_unit_id(
    session: Session, business: Business,
    default_unit: BusinessUnit,
) -> None:
    role = HiringService(session).hire(
        business_id=business.id,
        role_type=RoleType.WORKER,
        title="test worker",
        specialty="x",
        business_unit_id=default_unit.id,
    )
    assert role.business_unit_id == default_unit.id


def test_hiring_existing_csuite_unit_scoped_lazy_assignment(
    session: Session, business: Business,
    default_unit: BusinessUnit,
) -> None:
    """Hiring CEO twice doesn't dup the role. If first hire had no
    unit and second specifies one, the existing row gets scoped."""
    svc = HiringService(session)
    ceo_first = svc.hire(
        business_id=business.id, role_type=RoleType.CEO, title="CEO",
    )
    assert ceo_first.business_unit_id is None
    ceo_second = svc.hire(
        business_id=business.id, role_type=RoleType.CEO,
        business_unit_id=default_unit.id,
    )
    assert ceo_first.id == ceo_second.id
    assert ceo_second.business_unit_id == default_unit.id


# ---------------------------------------------------------------------------
# hr.start_business_line hires the Line VP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_business_line_hires_line_vp(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["hr.start_business_line"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"kind": "kdp"},
    )
    assert "owner_agent_role_id" in result.payload
    owner_id = result.payload["owner_agent_role_id"]

    owner = session.get(AgentRole, _U(owner_id))
    assert owner is not None
    assert owner.role_type == RoleType.WORKER
    assert "Line VP" in owner.title
    assert "KDP" in owner.title
    assert owner.specialty == "kdp-line-vp"
    assert owner.business_unit_id == _U(result.payload["unit_id"])

    # Unit's owner_agent_role_id wired
    unit = session.get(BusinessUnit, _U(result.payload["unit_id"]))
    assert unit.owner_agent_role_id == owner.id


@pytest.mark.asyncio
async def test_spawn_type_manager_hires_type_mgr(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.spawn_type_manager"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"parent_unit_id": str(line.id), "name": "Romance"},
    )
    owner = session.get(AgentRole, _U(result.payload["owner_agent_role_id"]))
    assert owner is not None
    assert "Type Manager" in owner.title
    assert "Romance" in owner.title
    assert owner.specialty == "type-owner"


@pytest.mark.asyncio
async def test_spawn_audience_manager_hires_audience_mgr(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="Affiliate",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.spawn_audience_manager"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "parent_unit_id": str(line.id),
            "name": "AI marketers",
            "niche_profile": {
                "core_topics": ["ai_marketing"],
            },
        },
    )
    owner = session.get(AgentRole, _U(result.payload["owner_agent_role_id"]))
    assert owner is not None
    assert "Audience Manager" in owner.title


@pytest.mark.asyncio
async def test_spawn_product_vp_hires_product_vp(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    board = BusinessUnitBoard(session)
    line = board.create(
        business_id=business.id, name="SaaS",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    skill = default_registry.skills["hr.spawn_product_vp"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "parent_unit_id": str(line.id), "name": "Korpha",
        },
    )
    owner = session.get(AgentRole, _U(result.payload["owner_agent_role_id"]))
    assert owner is not None
    assert "Product VP" in owner.title
    assert "Korpha" in owner.title


@pytest.mark.asyncio
async def test_multiple_lines_distinct_owners(
    session: Session, business: Business, founder: Founder,
    default_unit: BusinessUnit,
) -> None:
    """Two Lines get two distinct AgentRoles (KDP VP ≠ POD VP)."""
    skill = default_registry.skills["hr.start_business_line"]
    r1 = await skill.run(
        ctx=_ctx(session, business, founder), args={"kind": "kdp"},
    )
    r2 = await skill.run(
        ctx=_ctx(session, business, founder), args={"kind": "pod"},
    )
    assert r1.payload["owner_agent_role_id"] != r2.payload["owner_agent_role_id"]


def _U(s):
    from uuid import UUID
    return UUID(str(s))
