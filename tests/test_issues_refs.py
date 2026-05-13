"""Per-business issue ref allocation + formatting + lookup."""
from __future__ import annotations

from sqlmodel import Session

from korpha.business.issues import (
    allocate_task_ref,
    backfill_refs,
    business_prefix,
    find_task_by_ref,
    format_ref,
    parse_ref,
)
from korpha.business.model import Business, Task
from korpha.identity.model import Founder


def _biz(name: str) -> Business:
    return Business(name=name, description="")


def test_business_prefix_multiword_initials() -> None:
    assert business_prefix(_biz("Korpha")) == "KOR"
    assert business_prefix(_biz("Foo Bar Baz")) == "FBB"
    assert business_prefix(_biz("Solo Python Devs Inc")) == "SPDI"


def test_business_prefix_single_word() -> None:
    assert business_prefix(_biz("widget")) == "WID"
    assert business_prefix(_biz("Stripe")) == "STR"


def test_business_prefix_short_or_empty() -> None:
    assert business_prefix(_biz("")) == "ISS"
    assert business_prefix(_biz("AB")) == "AB"
    assert business_prefix(_biz("X")) == "ISS"


def test_business_prefix_strips_punctuation() -> None:
    assert business_prefix(_biz("Co. & Co.")) == "CC"


def test_format_ref_with_number(business: Business) -> None:
    assert format_ref(business, 42).endswith("-42")


def test_format_ref_legacy_no_number(business: Business) -> None:
    assert format_ref(business, None).endswith("-?")


def test_parse_ref_valid() -> None:
    assert parse_ref("AIG-42") == ("AIG", 42)
    assert parse_ref("aig-1") == ("AIG", 1)
    assert parse_ref("  WID-7  ") == ("WID", 7)


def test_parse_ref_invalid() -> None:
    assert parse_ref("nope") is None
    assert parse_ref("AIG") is None
    assert parse_ref("AIG-") is None


def test_allocate_starts_at_one(session: Session, business: Business) -> None:
    assert allocate_task_ref(session, business.id) == 1


def test_allocate_increments(session: Session, business: Business) -> None:
    n1 = allocate_task_ref(session, business.id)
    t = Task(business_id=business.id, title="first", ref_number=n1)
    session.add(t)
    session.commit()
    assert allocate_task_ref(session, business.id) == 2


def test_allocate_per_business_independent(
    session: Session, business: Business, founder: Founder
) -> None:
    other = Business(founder_id=founder.id, name="OtherCo", description="")
    session.add(other)
    session.commit()
    session.refresh(other)

    t = Task(business_id=business.id, title="a", ref_number=1)
    session.add(t)
    session.commit()
    assert allocate_task_ref(session, business.id) == 2
    assert allocate_task_ref(session, other.id) == 1


def test_find_task_by_ref(session: Session, business: Business) -> None:
    t = Task(business_id=business.id, title="findme", ref_number=7)
    session.add(t)
    session.commit()
    found = find_task_by_ref(
        session, business, format_ref(business, 7)
    )
    assert found is not None
    assert found.id == t.id


def test_find_task_by_ref_wrong_prefix_returns_none(
    session: Session, business: Business
) -> None:
    t = Task(business_id=business.id, title="x", ref_number=3)
    session.add(t)
    session.commit()
    assert find_task_by_ref(session, business, "ZZZ-3") is None


def test_find_task_by_ref_unknown_number(
    session: Session, business: Business
) -> None:
    assert find_task_by_ref(session, business, format_ref(business, 999)) is None


def test_find_task_by_ref_garbage(session: Session, business: Business) -> None:
    assert find_task_by_ref(session, business, "not a ref") is None


def test_backfill_assigns_in_created_order(
    session: Session, business: Business
) -> None:
    """Two ref-less tasks → 1, 2 in created order."""
    from datetime import timedelta

    from korpha.db._base import utcnow

    t1 = Task(business_id=business.id, title="older")
    t2 = Task(business_id=business.id, title="newer")
    session.add(t1)
    session.add(t2)
    session.commit()
    session.refresh(t1)
    session.refresh(t2)
    # Force chronological order regardless of insert lag.
    t1.created_at = utcnow() - timedelta(seconds=10)
    t2.created_at = utcnow()
    session.add(t1)
    session.add(t2)
    session.commit()

    n = backfill_refs(session, business.id)
    assert n == 2
    session.refresh(t1)
    session.refresh(t2)
    assert t1.ref_number == 1
    assert t2.ref_number == 2


def test_backfill_skips_existing(session: Session, business: Business) -> None:
    t1 = Task(business_id=business.id, title="has-ref", ref_number=5)
    t2 = Task(business_id=business.id, title="needs-ref")
    session.add(t1)
    session.add(t2)
    session.commit()
    n = backfill_refs(session, business.id)
    assert n == 1
    session.refresh(t1)
    session.refresh(t2)
    assert t1.ref_number == 5
    assert t2.ref_number == 6  # picked up where the existing one left off


def test_backfill_idempotent(session: Session, business: Business) -> None:
    t = Task(business_id=business.id, title="x")
    session.add(t)
    session.commit()
    backfill_refs(session, business.id)
    n2 = backfill_refs(session, business.id)
    assert n2 == 0
