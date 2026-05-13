"""Tests for the kanban board model + service."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole
from korpha.identity.model import Founder
from korpha.kanban.board import (
    CreateCardInput,
    KanbanBoard,
    KanbanError,
)
from korpha.kanban.model import (
    CardPriority,
    KanbanCard,
    KanbanCardEvent,
    KanbanColumn,
)


# ---- create ----


def test_create_card_lands_in_backlog(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id,
        title="Launch landing page",
        body="Stripe checkout, fake-door pricing tiers",
        created_by_founder_id=founder.id,
    ))
    assert card.id is not None
    assert card.column == KanbanColumn.BACKLOG
    assert card.title == "Launch landing page"
    assert card.created_by_founder_id == founder.id


def test_create_records_event_log(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    history = board.history(card.id)
    assert len(history) == 1
    assert history[0].kind == "create"
    assert history[0].to_column == KanbanColumn.BACKLOG


def test_create_blank_title_rejected(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    with pytest.raises(KanbanError, match="title required"):
        board.create(CreateCardInput(business_id=business.id, title="   "))


# ---- transitions ----


def test_move_backlog_to_specify_allowed(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    moved = board.move(
        card.id, KanbanColumn.SPECIFY, actor_founder_id=founder.id,
    )
    assert moved.column == KanbanColumn.SPECIFY


def test_move_backlog_to_in_progress_rejected(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Cards must flow through SPECIFY + READY first — no shortcuts."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    with pytest.raises(KanbanError, match="cannot move"):
        board.move(card.id, KanbanColumn.IN_PROGRESS)


def test_move_specify_to_ready_blocked_without_criteria(
    session: Session, business: Business, founder: Founder,
) -> None:
    """The SPECIFY gate stops half-baked cards from being claimable."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.move(card.id, KanbanColumn.SPECIFY)
    with pytest.raises(KanbanError, match="acceptance_criteria"):
        board.move(card.id, KanbanColumn.READY)


def test_specify_then_ready_works(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id,
        acceptance_criteria=["page deployed", "Stripe button works"],
        owner_role="cto",
    )
    moved = board.move(card.id, KanbanColumn.READY)
    assert moved.column == KanbanColumn.READY
    assert moved.acceptance_criteria == [
        "page deployed", "Stripe button works",
    ]
    assert moved.owner_role == "cto"


def test_specify_blocks_from_done(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cto",
    )
    board.move(card.id, KanbanColumn.READY)
    # done is not reachable from READY without going through claim + review
    with pytest.raises(KanbanError, match="cannot move"):
        board.move(card.id, KanbanColumn.DONE)


def test_specify_requires_owner_for_ready(
    session: Session, business: Business, founder: Founder,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(card.id, acceptance_criteria=["a"])
    with pytest.raises(KanbanError, match="owner_role"):
        board.move(card.id, KanbanColumn.READY)


def test_specify_rejects_empty_criteria(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    with pytest.raises(KanbanError, match="at least one"):
        board.specify(card.id, acceptance_criteria=[])


def test_specify_strips_whitespace_criteria(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    spec = board.specify(
        card.id,
        acceptance_criteria=["  test 1  ", "", "   test 2"],
    )
    assert spec.acceptance_criteria == ["test 1", "test 2"]


# ---- claim ----


def test_claim_ready_card_moves_to_in_progress(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    claimed = board.claim(
        card.id, agent_role_id=cmo.id, actor_role="cmo",
    )
    assert claimed.column == KanbanColumn.IN_PROGRESS
    assert claimed.claimed_by_agent_role_id == cmo.id
    assert claimed.claimed_at is not None


def test_claim_non_ready_rejected(
    session: Session, business: Business, ceo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    with pytest.raises(KanbanError, match="only READY"):
        board.claim(card.id, agent_role_id=ceo.id)


def test_claim_already_claimed_rejected(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole, ceo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")
    # After claim, card is in IN_PROGRESS — second claim attempt
    # trips the column-state guard, which is the right message.
    with pytest.raises(KanbanError, match="only READY"):
        board.claim(card.id, agent_role_id=ceo.id)


def test_claim_owner_role_mismatch_rejected(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    """A CMO-owned card shouldn't be claimable by a CTO impersonator."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    with pytest.raises(KanbanError, match="owned by cmo"):
        board.claim(card.id, agent_role_id=cmo.id, actor_role="cto")


# ---- review evidence ----


def test_submit_evidence_moves_to_review(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")
    reviewed = board.submit_review_evidence(
        card.id, evidence="Posted at https://example.com/post/1",
        actor_agent_role_id=cmo.id,
    )
    assert reviewed.column == KanbanColumn.REVIEW
    assert reviewed.review_evidence.startswith("Posted at")


def test_evidence_required_to_be_done(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    """REVIEW gate: can't accept without evidence."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")
    # Manually move to REVIEW (skipping evidence) — shouldn't be
    # possible via the normal API, so we patch the column directly
    # and verify the move-to-DONE check still trips.
    card_obj = session.get(KanbanCard, card.id)
    assert card_obj is not None
    card_obj.column = KanbanColumn.REVIEW
    session.add(card_obj)
    session.commit()
    with pytest.raises(KanbanError, match="review_evidence"):
        board.move(card.id, KanbanColumn.DONE)


def test_review_to_done_marks_accepted(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")
    board.submit_review_evidence(card.id, evidence="https://x.com")
    done = board.move(
        card.id, KanbanColumn.DONE, actor_founder_id=founder.id,
    )
    assert done.column == KanbanColumn.DONE
    assert done.review_verdict == "accepted"


def test_review_kickback_marks_rework(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    """Reviewer rejects: card goes back to IN_PROGRESS with verdict='rework'."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")
    board.submit_review_evidence(card.id, evidence="https://x.com")
    kicked = board.move(
        card.id, KanbanColumn.IN_PROGRESS, actor_founder_id=founder.id,
        note="evidence URL 404s",
    )
    assert kicked.column == KanbanColumn.IN_PROGRESS
    assert kicked.review_verdict == "rework"


# ---- listing ----


def test_board_snapshot_groups_by_column(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    a = board.create(CreateCardInput(
        business_id=business.id, title="A",
        created_by_founder_id=founder.id,
    ))
    b = board.create(CreateCardInput(
        business_id=business.id, title="B",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        b.id, acceptance_criteria=["x"], owner_role="cmo",
    )
    board.move(b.id, KanbanColumn.READY)

    snapshot = board.board_snapshot(business.id)
    assert KanbanColumn.ARCHIVED not in snapshot
    backlog_ids = {c.id for c in snapshot[KanbanColumn.BACKLOG]}
    ready_ids = {c.id for c in snapshot[KanbanColumn.READY]}
    assert a.id in backlog_ids
    assert b.id in ready_ids


def test_list_column_priority_ordering(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    low = board.create(CreateCardInput(
        business_id=business.id, title="low",
        priority=CardPriority.LOW,
    ))
    high = board.create(CreateCardInput(
        business_id=business.id, title="high",
        priority=CardPriority.HIGH,
    ))
    normal = board.create(CreateCardInput(
        business_id=business.id, title="normal",
        priority=CardPriority.NORMAL,
    ))
    cards = board.list_column(business.id, KanbanColumn.BACKLOG)
    assert [c.id for c in cards] == [high.id, normal.id, low.id]


# ---- audit log ----


def test_history_records_full_lifecycle(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")
    board.submit_review_evidence(card.id, evidence="ok")
    board.move(card.id, KanbanColumn.DONE)

    events = board.history(card.id)
    kinds = [e.kind for e in events]
    # create, specify, move (SPECIFY→READY), claim (which is also a
    # column move), review_evidence (REVIEW), move (REVIEW→DONE)
    assert kinds[0] == "create"
    assert "specify" in kinds
    assert "claim" in kinds
    assert "review_evidence" in kinds
    assert "move" in kinds
    assert kinds[-1] == "move"  # final REVIEW → DONE

    # all events scoped to this business + card
    for ev in events:
        assert ev.business_id == business.id
        assert ev.card_id == card.id


# ---- archive ----


def test_card_can_be_archived_from_any_column(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    archived = board.move(card.id, KanbanColumn.ARCHIVED)
    assert archived.column == KanbanColumn.ARCHIVED
    # snapshot omits archived
    assert KanbanColumn.ARCHIVED not in board.board_snapshot(business.id)


def test_unarchive_returns_to_backlog(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    board.move(card.id, KanbanColumn.ARCHIVED)
    back = board.move(card.id, KanbanColumn.BACKLOG)
    assert back.column == KanbanColumn.BACKLOG


# ---- idempotence + edge cases ----


def test_move_to_same_column_is_noop(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))
    same = board.move(card.id, KanbanColumn.BACKLOG)
    assert same.column == KanbanColumn.BACKLOG
    # no extra log event for a no-op
    history = board.history(card.id)
    assert len(history) == 1  # just the create


def test_move_unknown_card_raises(session: Session) -> None:
    from uuid import uuid4
    board = KanbanBoard(session)
    with pytest.raises(KanbanError, match="not found"):
        board.move(uuid4(), KanbanColumn.SPECIFY)


def test_release_in_progress_drops_claim(
    session: Session, business: Business, founder: Founder,
    cmo: AgentRole,
) -> None:
    """Agent can release a card back to READY (rejecting work)."""
    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="x",
        created_by_founder_id=founder.id,
    ))
    board.specify(
        card.id, acceptance_criteria=["a"], owner_role="cmo",
    )
    board.move(card.id, KanbanColumn.READY)
    board.claim(card.id, agent_role_id=cmo.id, actor_role="cmo")
    released = board.move(card.id, KanbanColumn.READY)
    assert released.column == KanbanColumn.READY
    assert released.claimed_by_agent_role_id is None
    assert released.claimed_at is None
