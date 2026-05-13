"""Tests for the kanban liveness classifier."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.kanban import CreateCardInput, KanbanBoard
from korpha.kanban.model import (
    KanbanCard, KanbanCardEvent, KanbanColumn,
)
from korpha.liveness import (
    StuckKind, classify_kanban_signals, default_thresholds,
)


# ---- helpers ----


def _hire_cto(session: Session, business: Business) -> AgentRole:
    role = AgentRole(
        business_id=business.id, role_type=RoleType.CTO, title="CTO",
    )
    session.add(role); session.commit(); session.refresh(role)
    return role


def _make_in_progress_card(
    session: Session, business: Business,
    *,
    title: str,
    moved_hours_ago: float,
    cto: AgentRole,
) -> KanbanCard:
    """Create + move a card to IN_PROGRESS, then back-date moved_at."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title=title,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cto",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cto.id, actor_role="cto")

    # Back-date moved_at
    card.moved_at = (
        datetime.now(tz=timezone.utc)
        - timedelta(hours=moved_hours_ago)
    )
    session.add(card); session.commit(); session.refresh(card)
    return card


def _make_review_card(
    session: Session, business: Business,
    *,
    title: str,
    moved_hours_ago: float,
    cto: AgentRole,
) -> KanbanCard:
    card = _make_in_progress_card(
        session, business, title=title,
        moved_hours_ago=0, cto=cto,
    )
    board = KanbanBoard(session)
    board.submit_review_evidence(
        card.id, evidence="https://example.com",
    )
    card = session.get(KanbanCard, card.id)
    card.moved_at = (
        datetime.now(tz=timezone.utc)
        - timedelta(hours=moved_hours_ago)
    )
    session.add(card); session.commit(); session.refresh(card)
    return card


# ---- IDLE_IN_PROGRESS ----


def test_idle_card_below_threshold_not_flagged(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_in_progress_card(
        session, business, title="fresh", moved_hours_ago=2.0,
        cto=cto,
    )
    assert classify_kanban_signals(session, business.id) == []


def test_idle_card_above_threshold_flagged(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_in_progress_card(
        session, business, title="wedged",
        moved_hours_ago=8.0, cto=cto,
    )
    signals = classify_kanban_signals(session, business.id)
    assert len(signals) == 1
    assert signals[0].kind == StuckKind.IDLE_IN_PROGRESS
    assert signals[0].severity == "warning"
    assert signals[0].age_hours >= 8.0
    assert "wedged" in signals[0].title


def test_idle_card_three_x_threshold_critical(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_in_progress_card(
        session, business, title="long-dead",
        moved_hours_ago=20.0, cto=cto,
    )
    signals = classify_kanban_signals(session, business.id)
    assert signals[0].severity == "critical"


def test_idle_summary_mentions_owner_role(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_in_progress_card(
        session, business, title="stuck",
        moved_hours_ago=10.0, cto=cto,
    )
    signals = classify_kanban_signals(session, business.id)
    assert "(CTO)" in signals[0].summary


# ---- REVIEW_OVERDUE ----


def test_review_within_grace_not_flagged(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_review_card(
        session, business, title="recent",
        moved_hours_ago=12.0, cto=cto,
    )
    assert classify_kanban_signals(session, business.id) == []


def test_review_past_two_days_flagged(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_review_card(
        session, business, title="forgotten",
        moved_hours_ago=72.0, cto=cto,
    )
    signals = classify_kanban_signals(session, business.id)
    assert len(signals) == 1
    assert signals[0].kind == StuckKind.REVIEW_OVERDUE
    assert "forgotten" in signals[0].title


def test_review_past_six_days_critical(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_review_card(
        session, business, title="critically-old",
        moved_hours_ago=200.0, cto=cto,
    )
    signals = classify_kanban_signals(session, business.id)
    assert signals[0].severity == "critical"


# ---- REWORK_LOOP ----


def test_rework_one_bounce_not_flagged(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    card = _make_review_card(
        session, business, title="single bounce",
        moved_hours_ago=1.0, cto=cto,
    )
    board = KanbanBoard(session)
    # Single REVIEW → IN_PROGRESS kickback
    board.move(
        card.id, KanbanColumn.IN_PROGRESS, note="rework",
    )
    signals = classify_kanban_signals(session, business.id)
    assert all(s.kind != StuckKind.REWORK_LOOP for s in signals)


def test_rework_two_bounces_flagged(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    card = _make_review_card(
        session, business, title="double bounce",
        moved_hours_ago=1.0, cto=cto,
    )
    board = KanbanBoard(session)
    # First bounce
    board.move(card.id, KanbanColumn.IN_PROGRESS, note="rework #1")
    board.submit_review_evidence(
        card.id, evidence="https://take2.com",
    )
    # Second bounce
    board.move(card.id, KanbanColumn.IN_PROGRESS, note="rework #2")
    signals = classify_kanban_signals(session, business.id)
    rework = [s for s in signals if s.kind == StuckKind.REWORK_LOOP]
    assert len(rework) == 1
    assert rework[0].extra["bounce_count"] == 2


def test_rework_three_bounces_critical(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    card = _make_review_card(
        session, business, title="triple",
        moved_hours_ago=1.0, cto=cto,
    )
    board = KanbanBoard(session)
    for i in range(1, 4):
        board.move(
            card.id, KanbanColumn.IN_PROGRESS, note=f"rework #{i}",
        )
        if i < 3:
            board.submit_review_evidence(
                card.id, evidence=f"https://take{i+1}.com",
            )
    signals = classify_kanban_signals(session, business.id)
    rework = [s for s in signals if s.kind == StuckKind.REWORK_LOOP]
    assert rework
    assert rework[0].severity == "critical"


def test_archived_card_with_bounces_not_flagged(
    session: Session, business: Business,
) -> None:
    """An archived card shouldn't keep showing up in liveness."""
    cto = _hire_cto(session, business)
    card = _make_review_card(
        session, business, title="archived bouncer",
        moved_hours_ago=1.0, cto=cto,
    )
    board = KanbanBoard(session)
    board.move(card.id, KanbanColumn.IN_PROGRESS, note="rework #1")
    board.submit_review_evidence(card.id, evidence="https://x.com")
    board.move(card.id, KanbanColumn.IN_PROGRESS, note="rework #2")
    # Now archive it
    board.move(card.id, KanbanColumn.ARCHIVED)

    signals = classify_kanban_signals(session, business.id)
    assert not any(
        s.kind == StuckKind.REWORK_LOOP for s in signals
    )


# ---- ordering ----


def test_critical_signals_sort_first(
    session: Session, business: Business,
) -> None:
    cto = _hire_cto(session, business)
    _make_in_progress_card(
        session, business, title="warning-only",
        moved_hours_ago=8.0, cto=cto,
    )
    _make_in_progress_card(
        session, business, title="critical-now",
        moved_hours_ago=25.0, cto=cto,
    )
    signals = classify_kanban_signals(session, business.id)
    assert len(signals) == 2
    assert signals[0].severity == "critical"
    assert signals[1].severity == "warning"


# ---- isolation ----


def test_isolates_by_business(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Cards from another business never bleed in."""
    cto = _hire_cto(session, business)
    _make_in_progress_card(
        session, business, title="ours",
        moved_hours_ago=10.0, cto=cto,
    )
    other = Business(
        founder_id=founder.id, name="Other", description="",
    )
    session.add(other); session.commit(); session.refresh(other)
    other_cto = AgentRole(
        business_id=other.id, role_type=RoleType.CTO, title="CTO",
    )
    session.add(other_cto); session.commit(); session.refresh(other_cto)
    _make_in_progress_card(
        session, other, title="theirs",
        moved_hours_ago=10.0, cto=other_cto,
    )

    signals = classify_kanban_signals(session, business.id)
    titles = {s.title for s in signals}
    assert "ours" in titles
    assert "theirs" not in titles


# ---- empty board ----


def test_empty_board_returns_empty(
    session: Session, business: Business,
) -> None:
    assert classify_kanban_signals(session, business.id) == []


# ---- thresholds tunable ----


def test_custom_thresholds_loosen_detection(
    session: Session, business: Business,
) -> None:
    """Bumping idle threshold to 24h means an 8h-old card is fine."""
    from korpha.liveness.classifier import Thresholds

    cto = _hire_cto(session, business)
    _make_in_progress_card(
        session, business, title="moderate",
        moved_hours_ago=8.0, cto=cto,
    )
    signals = classify_kanban_signals(
        session, business.id,
        thresholds=Thresholds(idle_in_progress_hours=24.0),
    )
    assert signals == []
