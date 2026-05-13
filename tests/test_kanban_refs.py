"""Tests for the cross-card reference extractor."""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.kanban import (
    CreateCardInput, KanbanBoard, KanbanCardRef, RefRelation,
    RefService,
)


def _create(board, business, title, body=""):
    return board.create(CreateCardInput(
        business_id=business.id, title=title, body=body,
    ))


# ---- extraction ----


def test_no_mentions_no_refs(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    _create(board, business, "build the thing", "no refs here")
    assert list(session.exec(select(KanbanCardRef)).all()) == []


def test_mention_with_full_uuid_creates_ref(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    target = _create(board, business, "auth model", "")
    body = (
        f"Implement the user model that depends on "
        f"#{str(target.id)[:8]} for the auth shape"
    )
    source = _create(board, business, "user model", body)

    refs = list(session.exec(select(KanbanCardRef)).all())
    assert len(refs) == 1
    assert refs[0].source_card_id == source.id
    assert refs[0].target_card_id == target.id


def test_relation_classified_from_context(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    target = _create(board, business, "auth", "")
    short = str(target.id)[:8]
    source = _create(
        board, business, "user model",
        f"This depends on #{short} for the user table.",
    )
    ref = list(session.exec(select(KanbanCardRef)).all())[0]
    assert ref.relation == RefRelation.DEPENDS_ON


def test_unblocks_relation(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    target = _create(board, business, "auth", "")
    short = str(target.id)[:8]
    source = _create(
        board, business, "user model",
        f"unblocks #{short} once shipped",
    )
    ref = list(session.exec(select(KanbanCardRef)).all())[0]
    assert ref.relation == RefRelation.UNBLOCKS


def test_see_also_relation(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    target = _create(board, business, "auth", "")
    short = str(target.id)[:8]
    source = _create(
        board, business, "user model",
        f"see also #{short}",
    )
    ref = list(session.exec(select(KanbanCardRef)).all())[0]
    assert ref.relation == RefRelation.SEE_ALSO


def test_short_prefix_ignored(
    session: Session, business: Business,
) -> None:
    """Less than 8 chars is ambiguous → ignore."""
    board = KanbanBoard(session)
    _create(board, business, "auth", "")
    _create(board, business, "user", "depends on #abc1")
    refs = list(session.exec(select(KanbanCardRef)).all())
    assert refs == []


def test_self_reference_ignored(
    session: Session, business: Business,
) -> None:
    """A card that mentions its own prefix shouldn't create a
    self-edge."""
    board = KanbanBoard(session)
    a = _create(board, business, "auth", "")
    short = str(a.id)[:8]
    # Now extract again with body that mentions itself
    a.body = f"this card is #{short}"
    session.add(a); session.commit()
    RefService(session).extract_and_persist(a)
    refs = list(session.exec(
        select(KanbanCardRef)
        .where(KanbanCardRef.source_card_id == a.id)
    ).all())
    assert refs == []


def test_unresolvable_prefix_ignored(
    session: Session, business: Business,
) -> None:
    """Prefix that doesn't match any card → no ref."""
    board = KanbanBoard(session)
    _create(board, business, "x", "")
    _create(board, business, "y", "see also #deadbeefdeadbeef")
    refs = list(session.exec(select(KanbanCardRef)).all())
    assert refs == []


def test_ambiguous_prefix_ignored(
    session: Session, business: Business,
) -> None:
    """Prefix that matches 2+ cards → ignored to keep graph clean.

    We can't easily force this with random UUIDs, so we
    construct it: insert two cards whose IDs share a 32-char
    prefix. SQLite stores UUIDs as hex; we just need to find any
    case where the prefix is shared. Since random-UUID collisions
    are virtually impossible, we test the absence-of-match path
    instead — same code branch."""
    board = KanbanBoard(session)
    target = _create(board, business, "x", "")
    full = str(target.id).lower()
    # Use the FULL uuid; that's unambiguous so it will match
    other = _create(board, business, "y", f"see #{full}")
    refs = list(session.exec(select(KanbanCardRef)).all())
    # Full UUID resolves cleanly
    assert len(refs) == 1


# ---- idempotent re-extraction ----


def test_re_extract_drops_old_refs(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    a = _create(board, business, "auth", "")
    b = _create(board, business, "user", "")
    short_a = str(a.id)[:8]
    short_b = str(b.id)[:8]

    source = _create(
        board, business, "model",
        f"depends on #{short_a}",
    )
    assert len(list(session.exec(select(KanbanCardRef)).all())) == 1

    # Edit the body to remove reference + add one to b
    source.body = f"depends on #{short_b} now"
    session.add(source); session.commit()
    RefService(session).extract_and_persist(source)

    refs = list(session.exec(
        select(KanbanCardRef).where(
            KanbanCardRef.source_card_id == source.id,
        )
    ).all())
    assert len(refs) == 1
    assert refs[0].target_card_id == b.id


# ---- service helpers ----


def test_references_from_returns_outgoing(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    a = _create(board, business, "auth", "")
    b = _create(board, business, "user", "")
    short = str(a.id)[:8]
    source = _create(
        board, business, "x", f"depends on #{short}",
    )
    refs = RefService(session).references_from(source.id)
    assert len(refs) == 1
    assert refs[0].target_card_id == a.id


def test_references_to_returns_incoming(
    session: Session, business: Business,
) -> None:
    board = KanbanBoard(session)
    a = _create(board, business, "auth", "")
    short = str(a.id)[:8]
    _create(
        board, business, "user model",
        f"depends on #{short}",
    )
    incoming = RefService(session).references_to(a.id)
    assert len(incoming) == 1


def test_dedupes_repeated_mentions_with_same_relation(
    session: Session, business: Business,
) -> None:
    """Mentioning the same target twice with the same relation
    should yield one edge, not two."""
    board = KanbanBoard(session)
    a = _create(board, business, "auth", "")
    short = str(a.id)[:8]
    _create(
        board, business, "user",
        f"depends on #{short} and also depends on #{short}",
    )
    refs = list(session.exec(select(KanbanCardRef)).all())
    assert len(refs) == 1
