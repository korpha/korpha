"""FTS5 message search tests."""
from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.fts import (
    ensure_fts_index,
    sanitize_fts5_query,
    search_messages,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import (
    Message,
    MessageSenderType,
    Thread,
    ThreadPlatform,
)
from korpha.db._base import utcnow
from korpha.identity.model import Founder


def _make_thread(
    session: Session, business: Business, founder: Founder
) -> Thread:
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    thread = Thread(
        business_id=business.id,
        founder_id=founder.id,
        agent_role_id=ceo.id,
        platform=ThreadPlatform.WEB,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def test_ensure_fts_index_idempotent(session: Session) -> None:
    assert ensure_fts_index(session) is True
    # Calling twice must not raise.
    assert ensure_fts_index(session) is True


def test_search_returns_empty_for_empty_query(
    session: Session, business: Business, founder: Founder
) -> None:
    ensure_fts_index(session)
    hits = search_messages(
        session, query="", business_id=business.id, founder_id=founder.id
    )
    assert hits == []


def test_search_finds_match_by_keyword(
    session: Session, business: Business, founder: Founder
) -> None:
    ensure_fts_index(session)
    thread = _make_thread(session, business, founder)
    base = utcnow()
    contents = [
        "we should ship a landing page first",
        "what should the pricing look like",
        "let's review last week's analytics dashboard",
    ]
    for i, c in enumerate(contents):
        m = Message(
            thread_id=thread.id,
            sender_type=MessageSenderType.FOUNDER,
            content=c,
        )
        m.created_at = base + timedelta(seconds=i)
        session.add(m)
    session.commit()

    hits = search_messages(
        session,
        query="pricing",
        business_id=business.id,
        founder_id=founder.id,
        limit=5,
    )
    assert len(hits) == 1
    assert "pricing" in hits[0].content


def test_search_scoped_to_founder_threads(
    session: Session, business: Business, founder: Founder
) -> None:
    ensure_fts_index(session)
    thread = _make_thread(session, business, founder)
    msg = Message(
        thread_id=thread.id,
        sender_type=MessageSenderType.FOUNDER,
        content="discussing the pricing problem",
    )
    session.add(msg)
    session.commit()

    # Different founder → no hits.
    other = Founder(email="other@example.com", display_name="Other")
    session.add(other)
    session.commit()
    session.refresh(other)

    hits = search_messages(
        session, query="pricing", business_id=business.id, founder_id=other.id
    )
    assert hits == []


def test_backfill_indexes_existing_messages(
    session: Session, business: Business, founder: Founder
) -> None:
    """Messages added BEFORE ensure_fts_index runs should be searchable."""
    thread = _make_thread(session, business, founder)
    msg = Message(
        thread_id=thread.id,
        sender_type=MessageSenderType.FOUNDER,
        content="we ought to validate niches before building",
    )
    session.add(msg)
    session.commit()

    # Now build the index — the trigger didn't exist when the row was inserted.
    ensure_fts_index(session)
    session.commit()

    hits = search_messages(
        session, query="validate", business_id=business.id, founder_id=founder.id
    )
    assert len(hits) == 1
    assert "validate" in hits[0].content


def test_sanitize_strips_unmatched_specials() -> None:
    assert "(" not in sanitize_fts5_query("hello (world")
    assert ")" not in sanitize_fts5_query("hello world)")
    assert "+" not in sanitize_fts5_query("hello+world")


def test_sanitize_preserves_balanced_quoted_phrase() -> None:
    out = sanitize_fts5_query('"exact phrase" something else')
    assert '"exact phrase"' in out


def test_sanitize_drops_dangling_boolean() -> None:
    assert sanitize_fts5_query("AND hello") == "hello"
    assert sanitize_fts5_query("hello OR") == "hello"


def test_sanitize_quotes_dotted_terms() -> None:
    out = sanitize_fts5_query("update my-app.config")
    assert '"my-app.config"' in out


def test_sanitize_empty_punctuation_collapses_to_empty() -> None:
    out = sanitize_fts5_query("()+{}^")
    assert out.strip() == ""


def test_search_handles_punctuation_only_query(
    session: Session, business: Business, founder: Founder
) -> None:
    ensure_fts_index(session)
    thread = _make_thread(session, business, founder)
    session.add(
        Message(
            thread_id=thread.id,
            sender_type=MessageSenderType.FOUNDER,
            content="real content here",
        )
    )
    session.commit()
    hits = search_messages(
        session, query="()+", business_id=business.id, founder_id=founder.id
    )
    assert hits == []


def test_search_orders_by_relevance(
    session: Session, business: Business, founder: Founder
) -> None:
    """Messages mentioning the query word more often should rank higher."""
    ensure_fts_index(session)
    thread = _make_thread(session, business, founder)
    base = utcnow()
    msgs = [
        ("just one mention of pricing", 0),
        ("pricing pricing pricing — definitely about pricing", 1),
        ("nothing relevant", 2),
    ]
    for content, offset in msgs:
        m = Message(
            thread_id=thread.id,
            sender_type=MessageSenderType.FOUNDER,
            content=content,
        )
        m.created_at = base + timedelta(seconds=offset)
        session.add(m)
    session.commit()

    hits = search_messages(
        session, query="pricing", business_id=business.id, founder_id=founder.id
    )
    assert len(hits) == 2
    # Top hit should be the one with more occurrences.
    assert hits[0].content.count("pricing") >= hits[1].content.count("pricing")
