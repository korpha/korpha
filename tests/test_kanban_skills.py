"""Tests for the agent-facing kanban skills."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole
from korpha.identity.model import Founder
from korpha.kanban import CreateCardInput, KanbanBoard
from korpha.kanban.model import KanbanColumn
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


def _ctx(
    session: Session, business: Business, founder: Founder,
    agent_role: AgentRole | None = None,
) -> SkillContext:
    """Build a SkillContext suitable for the kanban skills."""
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    pool = InferencePool(providers=[], accounts=[])
    return SkillContext(
        business=business,
        founder=founder,
        session=session,
        cost_tracker=CostTracker(pool=pool),
        invoking_agent_role_id=agent_role.id if agent_role else None,
    )


# ---- create_card ----


@pytest.mark.asyncio
async def test_create_card_lands_in_backlog(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    skill = default_registry.skills["kanban.create_card"]
    result = await skill.run(
        ctx=_ctx(session, business, founder, ceo),
        args={"title": "Launch landing page"},
    )
    assert result.payload["column"] == "backlog"
    assert result.payload["title"] == "Launch landing page"


@pytest.mark.asyncio
async def test_create_card_with_priority_and_owner(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    skill = default_registry.skills["kanban.create_card"]
    result = await skill.run(
        ctx=_ctx(session, business, founder, ceo),
        args={
            "title": "Critical fix",
            "priority": "high",
            "owner_role": "cto",
            "body": "auth header is missing on /api/health",
        },
    )
    assert result.payload["priority"] == "high"


@pytest.mark.asyncio
async def test_create_card_rejects_blank_title(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["kanban.create_card"]
    with pytest.raises(SkillError, match="title required"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"title": "  "},
        )


@pytest.mark.asyncio
async def test_create_card_rejects_long_title(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["kanban.create_card"]
    with pytest.raises(SkillError, match="too long"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"title": "x" * 250},
        )


@pytest.mark.asyncio
async def test_create_card_rejects_unknown_owner(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["kanban.create_card"]
    with pytest.raises(SkillError, match="owner_role"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"title": "x", "owner_role": "ninja"},
        )


@pytest.mark.asyncio
async def test_create_card_rejects_unknown_priority(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["kanban.create_card"]
    with pytest.raises(SkillError, match="priority"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"title": "x", "priority": "URGENT!!!"},
        )


# ---- specify_card ----


@pytest.mark.asyncio
async def test_specify_card_attaches_criteria_and_owner(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    skill = default_registry.skills["kanban.specify_card"]
    result = await skill.run(
        ctx=_ctx(session, business, founder, ceo),
        args={
            "card_id": str(card.id),
            "acceptance_criteria": [
                "page deployed", "Stripe button works",
            ],
            "owner_role": "cto",
        },
    )
    assert result.payload["criteria_count"] == 2
    assert result.payload["owner_role"] == "cto"
    # card moved BACKLOG → SPECIFY
    assert result.payload["column"] == "specify"


@pytest.mark.asyncio
async def test_specify_card_requires_list(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    skill = default_registry.skills["kanban.specify_card"]
    with pytest.raises(SkillError, match="must be a list"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "card_id": str(card.id),
                "acceptance_criteria": "just a string",
            },
        )


@pytest.mark.asyncio
async def test_specify_card_drops_blank_criteria(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    skill = default_registry.skills["kanban.specify_card"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "card_id": str(card.id),
            "acceptance_criteria": ["  test  ", "", "  ", "real"],
        },
    )
    assert result.payload["criteria_count"] == 2  # 'test' + 'real'


@pytest.mark.asyncio
async def test_specify_card_bad_uuid(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["kanban.specify_card"]
    with pytest.raises(SkillError, match="UUID"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "card_id": "not-a-uuid",
                "acceptance_criteria": ["x"],
            },
        )


# ---- move_card ----


@pytest.mark.asyncio
async def test_move_card_to_specify(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    skill = default_registry.skills["kanban.move_card"]
    result = await skill.run(
        ctx=_ctx(session, business, founder, ceo),
        args={"card_id": str(card.id), "to_column": "specify"},
    )
    assert result.payload["column"] == "specify"


@pytest.mark.asyncio
async def test_move_card_invalid_transition(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    skill = default_registry.skills["kanban.move_card"]
    with pytest.raises(SkillError, match="cannot move"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"card_id": str(card.id), "to_column": "done"},
        )


@pytest.mark.asyncio
async def test_move_card_unknown_column(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    skill = default_registry.skills["kanban.move_card"]
    with pytest.raises(SkillError, match="unknown column"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"card_id": str(card.id), "to_column": "nonsense"},
        )


# ---- claim_card ----


@pytest.mark.asyncio
async def test_claim_card_success(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    board.specify(card.id, acceptance_criteria=["a"], owner_role="cmo")
    board.move(card.id, KanbanColumn.READY)

    skill = default_registry.skills["kanban.claim_card"]
    result = await skill.run(
        ctx=_ctx(session, business, founder, cmo),
        args={"card_id": str(card.id)},
    )
    assert result.payload["column"] == "in_progress"
    assert result.payload["claimed_by_agent_role_id"] == str(cmo.id)


@pytest.mark.asyncio
async def test_claim_card_owner_mismatch(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    """CEO can't claim a CMO-owned card."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    board.specify(card.id, acceptance_criteria=["a"], owner_role="cmo")
    board.move(card.id, KanbanColumn.READY)

    skill = default_registry.skills["kanban.claim_card"]
    with pytest.raises(SkillError, match="owned by cmo"):
        await skill.run(
            ctx=_ctx(session, business, founder, ceo),
            args={"card_id": str(card.id)},
        )


@pytest.mark.asyncio
async def test_claim_without_invoking_role_rejected(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    skill = default_registry.skills["kanban.claim_card"]
    with pytest.raises(SkillError, match="invoking agent role"):
        await skill.run(
            ctx=_ctx(session, business, founder),  # no agent_role
            args={"card_id": str(card.id)},
        )


# ---- submit_evidence ----


@pytest.mark.asyncio
async def test_submit_evidence_moves_to_review(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    board.specify(card.id, acceptance_criteria=["a"], owner_role="cmo")
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")

    skill = default_registry.skills["kanban.submit_evidence"]
    result = await skill.run(
        ctx=_ctx(session, business, founder, cmo),
        args={
            "card_id": str(card.id),
            "evidence": "https://example.com/page",
        },
    )
    assert result.payload["column"] == "review"
    assert result.payload["evidence_chars"] > 0


@pytest.mark.asyncio
async def test_submit_evidence_blank_rejected(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    skill = default_registry.skills["kanban.submit_evidence"]
    with pytest.raises(SkillError, match="evidence required"):
        await skill.run(
            ctx=_ctx(session, business, founder, cmo),
            args={"card_id": str(uuid4()), "evidence": "  "},
        )


# ---- list_board ----


@pytest.mark.asyncio
async def test_list_board_returns_snapshot(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="A",
    ))
    board.create(CreateCardInput(
        business_id=business.id, title="B",
    ))

    skill = default_registry.skills["kanban.list_board"]
    result = await skill.run(
        ctx=_ctx(session, business, founder, ceo),
        args={},
    )
    assert result.payload["total"] == 2
    backlog_titles = [c["title"] for c in result.payload["snapshot"]["backlog"]]
    assert "A" in backlog_titles
    assert "B" in backlog_titles


@pytest.mark.asyncio
async def test_list_board_filters_to_one_column(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="A",
    ))

    skill = default_registry.skills["kanban.list_board"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"column": "ready"},
    )
    assert result.payload["column"] == "ready"
    assert result.payload["cards"] == []


@pytest.mark.asyncio
async def test_list_board_isolates_by_business(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Other-business cards don't leak into this board snapshot."""
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="ours",
    ))
    # Insert a foreign card directly
    from korpha.kanban.model import KanbanCard
    other = KanbanCard(
        business_id=uuid4(), title="theirs",
    )
    session.add(other); session.commit()

    skill = default_registry.skills["kanban.list_board"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={},
    )
    titles = [c["title"] for c in result.payload["snapshot"]["backlog"]]
    assert "ours" in titles
    assert "theirs" not in titles


@pytest.mark.asyncio
async def test_list_board_limit_per_column(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    for i in range(5):
        board.create(CreateCardInput(
            business_id=business.id, title=f"card-{i}",
        ))
    skill = default_registry.skills["kanban.list_board"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"limit_per_column": 2},
    )
    assert len(result.payload["snapshot"]["backlog"]) == 2


# ---- registry ----


def test_kanban_skills_registered() -> None:
    assert "kanban.create_card" in default_registry.skills
    assert "kanban.specify_card" in default_registry.skills
    assert "kanban.move_card" in default_registry.skills
    assert "kanban.claim_card" in default_registry.skills
    assert "kanban.submit_evidence" in default_registry.skills
    assert "kanban.list_board" in default_registry.skills
