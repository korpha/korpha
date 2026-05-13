"""Linear-style issue refs.

A Task gets a per-business sequential ``ref_number`` (1, 2, 3, …) and
the business contributes a 3-letter prefix derived from its name. Together
they make a stable human-readable handle like ``AIG-42`` that's recognizable
the way Linear tickets are.

The allocator queries ``MAX(ref_number)`` for the business at insert
time and increments. That's race-prone under concurrent writers but
fine for single-user use; bump to a Postgres sequence or advisory lock
if you ever run it under contention.
"""
from __future__ import annotations

import re
from uuid import UUID

from sqlmodel import Session, func, select

from korpha.business.model import Business, Task

_PREFIX_FALLBACK = "ISS"


def business_prefix(business: Business) -> str:
    """Compute the issue prefix from a business name.

    Strategy: take the alphanumeric letters from the name, prefer
    uppercase initials of multi-word names, otherwise the first three
    characters. Always returns a 2-4 char ``[A-Z0-9]`` string.
    """
    name = (business.name or "").strip()
    if not name:
        return _PREFIX_FALLBACK

    words = re.findall(r"[A-Za-z0-9]+", name)
    if len(words) >= 2:
        # Multi-word: take the first letter of each word, up to 4.
        initials = "".join(w[0] for w in words[:4]).upper()
        if len(initials) >= 2:
            return initials
    # Single word or fallback: first 3 alphanumerics, uppercased.
    flat = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    if len(flat) >= 3:
        return flat[:3]
    if len(flat) >= 2:
        return flat
    return _PREFIX_FALLBACK


def allocate_task_ref(session: Session, business_id: UUID) -> int:
    """Reserve the next sequential ref_number for ``business_id``.

    Caller is responsible for setting the returned value on the Task row
    they're inserting and committing in the same transaction.
    """
    current = session.exec(
        select(func.max(Task.ref_number)).where(Task.business_id == business_id)
    ).one_or_none()
    if current is None:
        return 1
    return int(current or 0) + 1


def format_ref(business: Business, ref_number: int | None) -> str:
    """Render the human ref string. Falls back to a UUID-derived stub
    for legacy tasks that predate the ref_number column."""
    if ref_number is not None and ref_number > 0:
        return f"{business_prefix(business)}-{ref_number}"
    return f"{business_prefix(business)}-?"


def parse_ref(ref: str) -> tuple[str, int] | None:
    """Parse an 'AIG-42' style ref. Returns (prefix, number) or None."""
    m = re.fullmatch(r"([A-Z0-9]{2,8})-(\d+)", ref.strip().upper())
    if m is None:
        return None
    return m.group(1), int(m.group(2))


def find_task_by_ref(
    session: Session, business: Business, ref: str
) -> Task | None:
    """Resolve an 'AIG-42' style ref against this business. Returns None
    when the prefix doesn't match or no task with that number exists."""
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    prefix, number = parsed
    if prefix != business_prefix(business):
        return None
    return session.exec(
        select(Task)
        .where(Task.business_id == business.id)
        .where(Task.ref_number == number)
    ).one_or_none()


def backfill_refs(session: Session, business_id: UUID) -> int:
    """Assign ref_number to any task in this business that's missing one,
    in created_at order. Idempotent — already-assigned rows are skipped.
    Returns how many rows were updated."""
    rows = list(
        session.exec(
            select(Task)
            .where(Task.business_id == business_id)
            .where(Task.ref_number.is_(None))  # type: ignore[union-attr]
            .order_by(Task.created_at.asc())  # type: ignore[attr-defined]
        ).all()
    )
    if not rows:
        return 0
    next_n = allocate_task_ref(session, business_id)
    for row in rows:
        row.ref_number = next_n
        next_n += 1
        session.add(row)
    session.commit()
    return len(rows)


__all__ = [
    "allocate_task_ref",
    "backfill_refs",
    "business_prefix",
    "find_task_by_ref",
    "format_ref",
    "parse_ref",
]
