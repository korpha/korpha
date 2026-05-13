"""PR3 tests — business_unit_id FK columns + backfill on KanbanCard /
Goal / Approval / Activity / AgentRole / CostLog.

Strategy: create rows on all 6 target tables WITHOUT business_unit_id
(simulating pre-PR3 data), then run the backfill helper and verify
every row points at the right Business's default unit.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from korpha.approvals.model import (
    ActionClass, Approval, ApprovalStatus,
)
from korpha.audit.model import (
    Activity, ActorType, Cost, InferenceTier,
)
from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import BusinessUnit, BusinessUnitKind
from korpha.cofounder.model import AgentRole, RoleType
from korpha.goals.model import Goal, GoalStatus
from korpha.identity.model import Founder
from korpha.kanban.model import CardPriority, KanbanCard, KanbanColumn


def _load_pr3_backfill():
    repo_root = Path(__file__).resolve().parent.parent
    rev_path = (
        repo_root / "alembic" / "versions"
        / "9a2f1b7c8e30_business_unit_id_fks.py"
    )
    spec = importlib.util.spec_from_file_location(
        "pr3_migration", str(rev_path),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.backfill_business_unit_ids


@pytest.fixture
def pr3_backfill():
    return _load_pr3_backfill()


def _make_default_unit(
    session: Session, business: Business,
) -> BusinessUnit:
    board = BusinessUnitBoard(session)
    return board.create(
        business_id=business.id, name=business.name,
        kind=BusinessUnitKind.DEFAULT,
    )


def test_kanbancard_has_business_unit_id_column() -> None:
    """The field is wired on the model. Column appears in schema."""
    cols = {c.name for c in KanbanCard.__table__.columns}
    assert "business_unit_id" in cols


@pytest.mark.parametrize(
    "model_cls",
    [KanbanCard, Goal, Approval, Activity, Cost, AgentRole],
)
def test_business_unit_id_column_is_nullable(model_cls) -> None:
    """All 6 target columns are nullable for the backfill window."""
    col = model_cls.__table__.c["business_unit_id"]
    assert col.nullable is True


def test_backfill_points_kanban_card_at_default_unit(
    session: Session, engine: Engine, business: Business,
    pr3_backfill,
) -> None:
    unit = _make_default_unit(session, business)
    card = KanbanCard(
        business_id=business.id,
        title="pre-PR3 card",
        column=KanbanColumn.BACKLOG,
        priority=CardPriority.NORMAL,
    )
    # Force business_unit_id to None to simulate pre-backfill state
    card.business_unit_id = None
    session.add(card)
    session.commit()
    session.refresh(card)
    assert card.business_unit_id is None

    with engine.connect() as conn:
        counts = pr3_backfill(conn)
        conn.commit()

    assert counts["kanban_card"] >= 1
    session.refresh(card)
    assert card.business_unit_id == unit.id


def test_backfill_points_goal_at_default_unit(
    session: Session, engine: Engine, founder: Founder,
    business: Business, pr3_backfill,
) -> None:
    from korpha.cofounder.model import (
        AgentRole as _AgentRole, RoleType as _RoleType,
        Thread, ThreadPlatform,
    )
    unit = _make_default_unit(session, business)
    agent = _AgentRole(
        business_id=business.id, role_type=_RoleType.CEO, title="CEO",
    )
    session.add(agent); session.commit(); session.refresh(agent)
    thread = Thread(
        business_id=business.id, founder_id=founder.id,
        agent_role_id=agent.id, platform=ThreadPlatform.WEB,
    )
    session.add(thread); session.commit(); session.refresh(thread)
    goal = Goal(
        thread_id=thread.id, business_id=business.id,
        text="ship 10 customers", status=GoalStatus.ACTIVE,
    )
    goal.business_unit_id = None
    session.add(goal); session.commit(); session.refresh(goal)
    assert goal.business_unit_id is None

    with engine.connect() as conn:
        pr3_backfill(conn)
        conn.commit()

    session.refresh(goal)
    assert goal.business_unit_id == unit.id


def test_backfill_points_approval_at_default_unit(
    session: Session, engine: Engine, business: Business,
    ceo: AgentRole, pr3_backfill,
) -> None:
    unit = _make_default_unit(session, business)
    approval = Approval(
        business_id=business.id,
        agent_role_id=ceo.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="pre-PR3 approval",
        status=ApprovalStatus.PENDING,
    )
    approval.business_unit_id = None
    session.add(approval); session.commit(); session.refresh(approval)

    with engine.connect() as conn:
        pr3_backfill(conn)
        conn.commit()

    session.refresh(approval)
    assert approval.business_unit_id == unit.id


def test_backfill_points_activity_at_default_unit(
    session: Session, engine: Engine, business: Business,
    pr3_backfill,
) -> None:
    unit = _make_default_unit(session, business)
    a = Activity(
        business_id=business.id,
        actor_type=ActorType.SYSTEM,
        event_type="test",
        payload={},
    )
    a.business_unit_id = None
    session.add(a); session.commit(); session.refresh(a)

    with engine.connect() as conn:
        pr3_backfill(conn)
        conn.commit()

    session.refresh(a)
    assert a.business_unit_id == unit.id


def test_backfill_points_cost_at_default_unit(
    session: Session, engine: Engine, business: Business,
    pr3_backfill,
) -> None:
    unit = _make_default_unit(session, business)
    c = Cost(
        business_id=business.id,
        provider="deepseek", model="deepseek-chat",
        tier=InferenceTier.WORKHORSE,
        input_tokens=100, output_tokens=50,
        cost_usd=Decimal("0.001"),
    )
    c.business_unit_id = None
    session.add(c); session.commit(); session.refresh(c)

    with engine.connect() as conn:
        pr3_backfill(conn)
        conn.commit()

    session.refresh(c)
    assert c.business_unit_id == unit.id


def test_backfill_points_agent_role_at_default_unit(
    session: Session, engine: Engine, business: Business,
    pr3_backfill,
) -> None:
    unit = _make_default_unit(session, business)
    role = AgentRole(
        business_id=business.id, role_type=RoleType.CTO, title="CTO",
    )
    role.business_unit_id = None
    session.add(role); session.commit(); session.refresh(role)

    with engine.connect() as conn:
        pr3_backfill(conn)
        conn.commit()

    session.refresh(role)
    assert role.business_unit_id == unit.id


def test_backfill_is_idempotent(
    session: Session, engine: Engine, business: Business,
    pr3_backfill,
) -> None:
    """Re-running backfill on a DB where all rows already point at the
    right unit creates 0 new updates."""
    unit = _make_default_unit(session, business)
    card = KanbanCard(
        business_id=business.id, title="x",
        column=KanbanColumn.BACKLOG, priority=CardPriority.NORMAL,
    )
    session.add(card); session.commit()

    with engine.connect() as conn:
        first = pr3_backfill(conn)
        conn.commit()
    with engine.connect() as conn:
        second = pr3_backfill(conn)
        conn.commit()

    # First pass updates the card; second pass updates 0
    assert first["kanban_card"] >= 1
    assert second["kanban_card"] == 0


def test_backfill_respects_existing_unit_assignments(
    session: Session, engine: Engine, business: Business,
    pr3_backfill,
) -> None:
    """Rows that already have a non-null business_unit_id from agent
    work (e.g. created post-PR3 by Line VP) are not overwritten."""
    board = BusinessUnitBoard(session)
    default_unit = board.create(
        business_id=business.id, name=business.name,
        kind=BusinessUnitKind.DEFAULT,
    )
    line_unit = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=default_unit.id,
    )
    # Card already scoped to the Line, not the default
    card = KanbanCard(
        business_id=business.id, title="kdp work",
        column=KanbanColumn.BACKLOG, priority=CardPriority.NORMAL,
        business_unit_id=line_unit.id,
    )
    session.add(card); session.commit(); session.refresh(card)
    assert card.business_unit_id == line_unit.id

    with engine.connect() as conn:
        pr3_backfill(conn)
        conn.commit()

    session.refresh(card)
    # Unchanged — the backfill only fills NULLs
    assert card.business_unit_id == line_unit.id


def test_backfill_only_fills_within_correct_business(
    session: Session, engine: Engine, founder: Founder,
    pr3_backfill,
) -> None:
    """Cards from Business A get A's default unit; Business B's get B's.
    No cross-business contamination."""
    biz_a = Business(
        founder_id=founder.id, name="A", description="x",
        founder_brief={},
    )
    biz_b = Business(
        founder_id=founder.id, name="B", description="y",
        founder_brief={},
    )
    session.add_all([biz_a, biz_b]); session.commit()
    session.refresh(biz_a); session.refresh(biz_b)

    board = BusinessUnitBoard(session)
    unit_a = board.create(
        business_id=biz_a.id, name="A",
        kind=BusinessUnitKind.DEFAULT,
    )
    unit_b = board.create(
        business_id=biz_b.id, name="B",
        kind=BusinessUnitKind.DEFAULT,
    )

    card_a = KanbanCard(
        business_id=biz_a.id, title="a-card",
        column=KanbanColumn.BACKLOG, priority=CardPriority.NORMAL,
    )
    card_b = KanbanCard(
        business_id=biz_b.id, title="b-card",
        column=KanbanColumn.BACKLOG, priority=CardPriority.NORMAL,
    )
    card_a.business_unit_id = None
    card_b.business_unit_id = None
    session.add_all([card_a, card_b]); session.commit()
    session.refresh(card_a); session.refresh(card_b)

    with engine.connect() as conn:
        pr3_backfill(conn)
        conn.commit()

    session.refresh(card_a); session.refresh(card_b)
    assert card_a.business_unit_id == unit_a.id
    assert card_b.business_unit_id == unit_b.id
    assert card_a.business_unit_id != card_b.business_unit_id
