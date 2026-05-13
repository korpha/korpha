"""PR-INT-15 tests — Workforce dispatcher routes kanban cards with
business_unit_id through the VP runner.

When a card has business_unit_id set + the unit has an owner agent,
Workforce._select_unit_vp_executor returns a VpExecutor instead of
the generic Director — same .attempt() interface so the dispatch
loop doesn't notice the difference."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import BusinessUnit, BusinessUnitKind
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.cofounder.vp_runner import VpExecutor
from korpha.identity.model import Founder
from korpha.kanban import CreateCardInput, KanbanBoard


# We reuse ScriptedProvider semantics from test_int13 — but keep this
# file self-contained by stubbing the minimum cost-tracker shape we need.
from tests.test_int13_vp_runner import (  # noqa: E402
    ScriptedProvider, _account, _make_tracker,
)


@pytest.fixture
def tree(session: Session, business: Business) -> dict[str, BusinessUnit]:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    kdp = board.create(
        business_id=business.id, name="Romance KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    if kdp.owner_agent_role_id is None:
        vp = HiringService(session).hire(
            business_id=business.id, role_type=RoleType.WORKER,
            title="Line VP: Romance KDP",
            specialty="kdp-line-vp",
            business_unit_id=kdp.id,
        )
        kdp.owner_agent_role_id = vp.id
        session.add(kdp); session.commit()
    session.refresh(kdp)
    return {"root": root, "kdp": kdp}


def _make_workforce(session: Session, provider: ScriptedProvider):
    """Build a minimal Workforce with one Director sharing the
    scripted CostTracker so the VP routing helper has something to
    pull cost_tracker from."""
    from korpha.cofounder.director import (
        DEFAULT_PERSONALITIES, Director,
    )
    from korpha.cofounder.workforce import Workforce
    from korpha.blockers.queue import BlockerQueue

    tracker = _make_tracker(provider)
    cto_personality = DEFAULT_PERSONALITIES[RoleType.CTO]
    director = Director(
        personality=cto_personality,
        session=session,
        cost_tracker=tracker,
        hiring=HiringService(session),
        queue=BlockerQueue(session),
    )
    return Workforce(directors={RoleType.CTO: director}), tracker


def test_unit_vp_executor_returned_when_card_has_unit(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """A kanban card in BACKLOG with business_unit_id set + a unit
    that has an owner → Workforce picks VpExecutor."""
    kb = KanbanBoard(session)
    card = kb.create(CreateCardInput(
        business_id=business.id,
        title="Plan KDP launch checklist",
        created_by_founder_id=founder.id,
    ))
    card.business_unit_id = tree["kdp"].id
    session.add(card); session.commit()

    provider = ScriptedProvider()
    workforce, _ = _make_workforce(session, provider)
    executor = workforce._select_unit_vp_executor(
        "Plan KDP launch checklist",
        business_id=business.id,
        session=session,
    )
    assert executor is not None
    assert isinstance(executor, VpExecutor)
    assert executor.unit_id == tree["kdp"].id


def test_no_vp_executor_when_card_has_no_unit(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Card without business_unit_id → falls through to regular
    director routing (helper returns None)."""
    kb = KanbanBoard(session)
    kb.create(CreateCardInput(
        business_id=business.id,
        title="Company-wide bookkeeping",
        created_by_founder_id=founder.id,
    ))
    provider = ScriptedProvider()
    workforce, _ = _make_workforce(session, provider)
    executor = workforce._select_unit_vp_executor(
        "Company-wide bookkeeping",
        business_id=business.id,
        session=session,
    )
    assert executor is None


def test_no_vp_executor_when_unit_has_no_owner(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Unit without owner_agent_role_id → falls through (can't
    delegate to nobody)."""
    # Wipe the owner
    tree["kdp"].owner_agent_role_id = None
    session.add(tree["kdp"]); session.commit()

    kb = KanbanBoard(session)
    card = kb.create(CreateCardInput(
        business_id=business.id, title="KDP work",
        created_by_founder_id=founder.id,
    ))
    card.business_unit_id = tree["kdp"].id
    session.add(card); session.commit()

    provider = ScriptedProvider()
    workforce, _ = _make_workforce(session, provider)
    executor = workforce._select_unit_vp_executor(
        "KDP work", business_id=business.id, session=session,
    )
    assert executor is None


def test_no_card_matching_title_returns_none(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """No kanban card with that title → None (workforce falls back
    to the existing task-text-based router)."""
    provider = ScriptedProvider()
    workforce, _ = _make_workforce(session, provider)
    executor = workforce._select_unit_vp_executor(
        "Some untracked task",
        business_id=business.id,
        session=session,
    )
    assert executor is None


def test_vp_executor_personality_quacks_like_director(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """The dispatcher reads executor.personality.role_type +
    .title for kanban bookkeeping — VpExecutor must expose these."""
    provider = ScriptedProvider()
    tracker = _make_tracker(provider)
    executor = VpExecutor(
        unit_id=tree["kdp"].id,
        session=session,
        cost_tracker=tracker,
    )
    p = executor.personality
    assert p.role_type == RoleType.WORKER
    assert "Romance KDP" in p.title


@pytest.mark.asyncio
async def test_vp_executor_attempt_returns_attempt_result(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """VpExecutor.attempt() returns the same AttemptResult shape
    Director.attempt() returns, so the dispatch loop can normalize
    it without special-casing."""
    from korpha.cofounder.director import AttemptResult

    provider = ScriptedProvider(rules=[
        ("Available skills:", '{"action":"respond","content":"VP acked"}'),
    ])
    tracker = _make_tracker(provider)
    executor = VpExecutor(
        unit_id=tree["kdp"].id,
        session=session,
        cost_tracker=tracker,
    )
    result = await executor.attempt(
        business=business, founder=founder,
        task="plan a thing",
    )
    assert isinstance(result, AttemptResult)
    assert result.status == "shipped"
    assert "VP acked" in result.summary
    assert "Romance KDP" in result.title
