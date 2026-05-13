"""Workforce ↔ kanban integration tests.

Verifies that when CEO has mirrored Plan tasks onto the board, the
workforce auto-advances each card BACKLOG → IN_PROGRESS at dispatch
start and to REVIEW (with evidence) when the director ships.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.cofounder.director import (
    DEFAULT_PERSONALITIES, AttemptResult, DirectorPersonality,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import RoleType
from korpha.cofounder.workforce import (
    Workforce, _kanban_advance_to_in_progress, _kanban_finalize,
    _strip_role_tag,
)
from korpha.identity.model import Founder
from korpha.kanban import CreateCardInput, KanbanBoard
from korpha.kanban.model import KanbanCard, KanbanColumn


# ---- _strip_role_tag ----


def test_strip_role_tag_removes_cto_prefix() -> None:
    assert _strip_role_tag("[CTO] deploy /pricing") == "deploy /pricing"


def test_strip_role_tag_handles_no_tag() -> None:
    assert _strip_role_tag("plain task") == "plain task"


def test_strip_role_tag_handles_colon_separator() -> None:
    assert _strip_role_tag("[CMO]: post update") == "post update"


def test_strip_role_tag_empty() -> None:
    assert _strip_role_tag("") == ""


# ---- _kanban_advance_to_in_progress ----


@dataclass
class _StubDirector:
    """Director just enough to slot into Workforce + carry a session
    + personality. ``attempt`` returns whatever was queued."""

    personality: DirectorPersonality
    session: Session
    queue_result: AttemptResult | None = None
    captured_tasks: list[str] = field(default_factory=list)
    cost_tracker: Any = None
    queue: Any = None
    hiring: Any = None

    async def attempt(
        self, *, business: Business, founder: Founder, task: str,
    ) -> AttemptResult:
        self.captured_tasks.append(task)
        if self.queue_result is not None:
            return self.queue_result
        return AttemptResult(
            role_type=self.personality.role_type,
            title=task[:50],
            status="shipped",
            summary=f"did the thing for: {task[:30]}",
            detail="did the work",
            blocker_ids=[],
            raw_response="raw",
            reasoning=None,
            cost_usd=0.001,
        )


def _hire_role(session: Session, business: Business, role: RoleType):
    from korpha.cofounder.model import AgentRole

    r = AgentRole(
        business_id=business.id, role_type=role, title=role.value.upper(),
    )
    session.add(r); session.commit(); session.refresh(r)
    return r


def test_advance_finds_card_and_moves_to_in_progress(
    session: Session, business: Business,
) -> None:
    cto = _hire_role(session, business, RoleType.CTO)
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="deploy /pricing",
    ))

    handle = _kanban_advance_to_in_progress(
        session=session,
        business_id=business.id,
        task_text="[CTO] deploy /pricing",
        role_type=RoleType.CTO,
    )
    assert handle is not None

    card = session.get(KanbanCard, handle.card_id)
    assert card is not None
    assert card.column == KanbanColumn.IN_PROGRESS
    assert card.claimed_by_agent_role_id == cto.id
    assert card.owner_role == "cto"
    assert card.acceptance_criteria == ["deploy /pricing"]


def test_advance_no_match_returns_none(
    session: Session, business: Business,
) -> None:
    """Workforce-dispatched task with no matching card is fine —
    the kanban path is opt-in via the CEO mirror."""
    handle = _kanban_advance_to_in_progress(
        session=session,
        business_id=business.id,
        task_text="some random task",
        role_type=RoleType.CMO,
    )
    assert handle is None


def test_advance_picks_newest_when_multiple_match(
    session: Session, business: Business,
) -> None:
    """If CEO ran a Plan twice with the same task title, prefer
    the newer card."""
    _hire_role(session, business, RoleType.CTO)
    board = KanbanBoard(session)
    older = board.create(CreateCardInput(
        business_id=business.id, title="ship feature X",
    ))
    newer = board.create(CreateCardInput(
        business_id=business.id, title="ship feature X",
    ))
    handle = _kanban_advance_to_in_progress(
        session=session,
        business_id=business.id,
        task_text="[CTO] ship feature X",
        role_type=RoleType.CTO,
    )
    assert handle is not None
    assert handle.card_id == newer.id
    # older one stays in BACKLOG
    older_after = session.get(KanbanCard, older.id)
    assert older_after.column == KanbanColumn.BACKLOG


def test_advance_no_role_hired_returns_none(
    session: Session, business: Business,
) -> None:
    """No active CMO role exists yet → can't claim, so we
    return None (no-op) rather than crashing."""
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="post update",
    ))
    handle = _kanban_advance_to_in_progress(
        session=session,
        business_id=business.id,
        task_text="[CMO] post update",
        role_type=RoleType.CMO,
    )
    assert handle is None


def test_advance_skips_when_session_is_none() -> None:
    handle = _kanban_advance_to_in_progress(
        session=None,
        business_id=UUID("00000000-0000-0000-0000-000000000001"),
        task_text="x",
        role_type=RoleType.CTO,
    )
    assert handle is None


# ---- _kanban_finalize ----


def test_finalize_shipped_attaches_evidence_and_moves_review(
    session: Session, business: Business,
) -> None:
    _hire_role(session, business, RoleType.CTO)
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="deploy",
    ))
    handle = _kanban_advance_to_in_progress(
        session=session, business_id=business.id,
        task_text="[CTO] deploy", role_type=RoleType.CTO,
    )
    assert handle is not None

    attempt = AttemptResult(
        role_type=RoleType.CTO, title="deploy",
        status="shipped",
        summary="deployed at https://example.com/pricing",
        detail="merged PR #42",
        blocker_ids=[], raw_response="r", reasoning=None, cost_usd=0.0,
    )
    _kanban_finalize(session=session, handle=handle, attempt=attempt)

    after = session.get(KanbanCard, card.id)
    assert after.column == KanbanColumn.REVIEW
    assert "https://example.com/pricing" in (after.review_evidence or "")
    assert "merged PR #42" in (after.review_evidence or "")


def test_finalize_blocked_releases_back_to_ready(
    session: Session, business: Business,
) -> None:
    """When a director comes back blocked, drop the claim so the
    board reflects the card needs another turn (or a different
    role) — don't pretend the work is in REVIEW."""
    _hire_role(session, business, RoleType.CTO)
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="deploy",
    ))
    handle = _kanban_advance_to_in_progress(
        session=session, business_id=business.id,
        task_text="[CTO] deploy", role_type=RoleType.CTO,
    )
    assert handle is not None
    attempt = AttemptResult(
        role_type=RoleType.CTO, title="deploy",
        status="blocked",
        summary="needs founder decision on pricing tiers",
        detail=None, blocker_ids=[], raw_response="",
        reasoning=None, cost_usd=0.0,
    )
    _kanban_finalize(session=session, handle=handle, attempt=attempt)

    after = session.get(KanbanCard, card.id)
    assert after.column == KanbanColumn.READY
    assert after.claimed_by_agent_role_id is None


def test_finalize_error_releases_to_ready(
    session: Session, business: Business,
) -> None:
    _hire_role(session, business, RoleType.CTO)
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="deploy",
    ))
    handle = _kanban_advance_to_in_progress(
        session=session, business_id=business.id,
        task_text="[CTO] deploy", role_type=RoleType.CTO,
    )
    attempt = AttemptResult(
        role_type=RoleType.CTO, title="deploy",
        status="error", summary="LLM timeout",
        detail=None, blocker_ids=[], raw_response="",
        reasoning=None, cost_usd=0.0,
    )
    _kanban_finalize(session=session, handle=handle, attempt=attempt)

    after = session.get(KanbanCard, card.id)
    assert after.column == KanbanColumn.READY


def test_finalize_no_handle_is_noop(
    session: Session, business: Business,
) -> None:
    """No card was matched at dispatch → finalize is no-op."""
    attempt = AttemptResult(
        role_type=RoleType.CTO, title="x", status="shipped",
        summary="ok", detail=None, blocker_ids=[],
        raw_response="", reasoning=None, cost_usd=0.0,
    )
    # Should not raise. No assertion needed beyond "didn't crash".
    _kanban_finalize(session=session, handle=None, attempt=attempt)


# ---- Workforce.dispatch end-to-end with kanban ----


@pytest.mark.asyncio
async def test_dispatch_advances_kanban_for_matched_tasks(
    session: Session, business: Business, founder: Founder,
) -> None:
    _hire_role(session, business, RoleType.CTO)
    _hire_role(session, business, RoleType.CMO)
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="deploy /pricing",
    ))
    board.create(CreateCardInput(
        business_id=business.id, title="post a tweet",
    ))

    cto_director = _StubDirector(
        personality=DEFAULT_PERSONALITIES[RoleType.CTO],
        session=session,
    )
    cmo_director = _StubDirector(
        personality=DEFAULT_PERSONALITIES[RoleType.CMO],
        session=session,
    )
    workforce = Workforce(
        directors={
            RoleType.CTO: cto_director,
            RoleType.CMO: cmo_director,
        },
    )

    results = await workforce.dispatch(
        business=business, founder=founder,
        tasks=["[CTO] deploy /pricing", "[CMO] post a tweet"],
    )
    assert len(results) == 2
    assert all(r.status == "shipped" for r in results)

    cards = list(session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).all())
    assert len(cards) == 2
    by_title = {c.title: c for c in cards}
    # Both should be in REVIEW with evidence
    for title in ("deploy /pricing", "post a tweet"):
        c = by_title[title]
        assert c.column == KanbanColumn.REVIEW
        assert c.review_evidence is not None


@pytest.mark.asyncio
async def test_dispatch_no_kanban_card_still_works(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Existing flows that don't pre-create kanban cards must keep
    working. The dispatch returns AttemptResults as before; no card
    rows show up after."""
    _hire_role(session, business, RoleType.CTO)

    cto_director = _StubDirector(
        personality=DEFAULT_PERSONALITIES[RoleType.CTO],
        session=session,
    )
    workforce = Workforce(directors={RoleType.CTO: cto_director})

    results = await workforce.dispatch(
        business=business, founder=founder,
        tasks=["[CTO] something not on the board"],
    )
    assert len(results) == 1
    assert results[0].status == "shipped"

    cards = list(session.exec(select(KanbanCard)).all())
    assert cards == []


@pytest.mark.asyncio
async def test_dispatch_blocked_returns_card_to_ready(
    session: Session, business: Business, founder: Founder,
) -> None:
    _hire_role(session, business, RoleType.CTO)
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="deploy /pricing",
    ))

    blocked_result = AttemptResult(
        role_type=RoleType.CTO, title="deploy /pricing",
        status="blocked", summary="needs Mike to pick a tier",
        detail=None, blocker_ids=[], raw_response="",
        reasoning=None, cost_usd=0.0,
    )
    cto = _StubDirector(
        personality=DEFAULT_PERSONALITIES[RoleType.CTO],
        session=session,
        queue_result=blocked_result,
    )
    workforce = Workforce(directors={RoleType.CTO: cto})

    await workforce.dispatch(
        business=business, founder=founder,
        tasks=["[CTO] deploy /pricing"],
    )
    card = session.exec(
        select(KanbanCard).where(
            KanbanCard.business_id == business.id,
        )
    ).one()
    assert card.column == KanbanColumn.READY
    assert card.claimed_by_agent_role_id is None
