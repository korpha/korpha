"""Tests for hr.* — agent-callable team management."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


def _ctx(session, business, founder, agent_role_id=None):
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    pool = InferencePool(providers=[], accounts=[])
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=pool),
        invoking_agent_role_id=agent_role_id,
    )


# ---- hire_worker ----


@pytest.mark.asyncio
async def test_hire_worker_creates_role(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.hire_worker"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "specialty": "copywriter",
            "reason": "we keep doing 5 LinkedIn drafts a week",
        },
    )
    assert result.payload["specialty"] == "copywriter"
    role_id = UUID(result.payload["agent_role_id"])
    role = session.get(AgentRole, role_id)
    assert role is not None
    assert role.role_type == RoleType.WORKER
    assert role.specialty == "copywriter"
    assert role.is_active is True


@pytest.mark.asyncio
async def test_hire_worker_default_title_titlecases_specialty(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.hire_worker"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"specialty": "ads-manager"},
    )
    role = session.get(AgentRole, UUID(result.payload["agent_role_id"]))
    assert role.title == "Ads Manager"


@pytest.mark.asyncio
async def test_hire_worker_explicit_title(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.hire_worker"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "specialty": "support-rep",
            "title": "Customer Champion",
        },
    )
    role = session.get(AgentRole, UUID(result.payload["agent_role_id"]))
    assert role.title == "Customer Champion"


@pytest.mark.asyncio
async def test_hire_worker_blank_specialty_rejected(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.hire_worker"]
    with pytest.raises(SkillError, match="specialty required"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"specialty": "  "},
        )


@pytest.mark.asyncio
async def test_hire_worker_rejects_specialty_with_spaces(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.hire_worker"]
    with pytest.raises(SkillError, match="one token"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"specialty": "copy writer"},
        )


@pytest.mark.asyncio
async def test_hire_worker_rejects_long_specialty(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.hire_worker"]
    with pytest.raises(SkillError, match="too long"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"specialty": "x" * 80},
        )


@pytest.mark.asyncio
async def test_hire_worker_can_hire_two_with_same_specialty(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Workers don't have the unique-active-role-per-business
    constraint that C-suite does; you can have 3 copywriters."""
    skill = default_registry.skills["hr.hire_worker"]
    a = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"specialty": "copywriter"},
    )
    b = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"specialty": "copywriter"},
    )
    assert a.payload["agent_role_id"] != b.payload["agent_role_id"]


# ---- fire_worker ----


@pytest.mark.asyncio
async def test_fire_worker_deactivates_role(
    session: Session, business: Business, founder: Founder,
) -> None:
    hiring = HiringService(session)
    role = hiring.hire(
        business.id, RoleType.WORKER,
        title="Copywriter", specialty="copywriter",
    )
    skill = default_registry.skills["hr.fire_worker"]
    await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "agent_role_id": str(role.id),
            "reason": "no output for 30 days",
        },
    )
    refreshed = session.get(AgentRole, role.id)
    assert refreshed is not None
    assert refreshed.is_active is False
    assert refreshed.fired_at is not None


@pytest.mark.asyncio
async def test_fire_worker_refuses_c_suite(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    """The CEO shouldn't be fireable via this skill."""
    skill = default_registry.skills["hr.fire_worker"]
    with pytest.raises(SkillError, match="role_type=ceo"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"agent_role_id": str(ceo.id)},
        )


@pytest.mark.asyncio
async def test_fire_worker_unknown_role(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.fire_worker"]
    with pytest.raises(SkillError, match="not found"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"agent_role_id": str(uuid4())},
        )


@pytest.mark.asyncio
async def test_fire_worker_cross_business_rejected(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Worker hired against business A → refused when B asks to fire."""
    other = Business(
        founder_id=founder.id, name="B", description="",
    )
    session.add(other); session.commit(); session.refresh(other)
    hiring = HiringService(session)
    role = hiring.hire(
        other.id, RoleType.WORKER,
        title="X", specialty="x",
    )
    skill = default_registry.skills["hr.fire_worker"]
    with pytest.raises(SkillError, match="different business"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"agent_role_id": str(role.id)},
        )


@pytest.mark.asyncio
async def test_fire_worker_bad_uuid(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["hr.fire_worker"]
    with pytest.raises(SkillError, match="bad UUID"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"agent_role_id": "not-a-uuid"},
        )


# ---- list_team ----


@pytest.mark.asyncio
async def test_list_team_includes_c_suite_and_workers(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    hiring = HiringService(session)
    hiring.hire(
        business.id, RoleType.WORKER,
        title="Copywriter", specialty="copywriter",
    )
    hiring.hire(
        business.id, RoleType.WORKER,
        title="Ads", specialty="ads-manager",
    )
    skill = default_registry.skills["hr.list_team"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={},
    )
    assert result.payload["c_suite_count"] >= 1  # ceo
    assert result.payload["worker_count"] == 2
    titles = {t["title"] for t in result.payload["team"]}
    assert "CEO" in titles
    assert "Copywriter" in titles
    assert "Ads" in titles


@pytest.mark.asyncio
async def test_list_team_excludes_inactive_by_default(
    session: Session, business: Business, founder: Founder,
) -> None:
    hiring = HiringService(session)
    role = hiring.hire(
        business.id, RoleType.WORKER,
        title="Departing", specialty="x",
    )
    hiring.fire(role.id, reason="test")

    skill = default_registry.skills["hr.list_team"]
    result = await skill.run(
        ctx=_ctx(session, business, founder), args={},
    )
    titles = {t["title"] for t in result.payload["team"]}
    assert "Departing" not in titles


@pytest.mark.asyncio
async def test_list_team_include_inactive(
    session: Session, business: Business, founder: Founder,
) -> None:
    hiring = HiringService(session)
    role = hiring.hire(
        business.id, RoleType.WORKER,
        title="Departing", specialty="x",
    )
    hiring.fire(role.id, reason="test")

    skill = default_registry.skills["hr.list_team"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"include_inactive": "true"},
    )
    titles = {t["title"] for t in result.payload["team"]}
    assert "Departing" in titles


@pytest.mark.asyncio
async def test_list_team_isolates_by_business(
    session: Session, business: Business, founder: Founder,
) -> None:
    other = Business(
        founder_id=founder.id, name="Other", description="",
    )
    session.add(other); session.commit(); session.refresh(other)
    HiringService(session).hire(
        other.id, RoleType.WORKER,
        title="Theirs", specialty="x",
    )
    HiringService(session).hire(
        business.id, RoleType.WORKER,
        title="Ours", specialty="x",
    )

    skill = default_registry.skills["hr.list_team"]
    result = await skill.run(
        ctx=_ctx(session, business, founder), args={},
    )
    titles = {t["title"] for t in result.payload["team"]}
    assert "Ours" in titles
    assert "Theirs" not in titles


def test_hr_skills_registered() -> None:
    assert "hr.hire_worker" in default_registry.skills
    assert "hr.fire_worker" in default_registry.skills
    assert "hr.list_team" in default_registry.skills
