"""Task links + comments for kanban cards.

Ports Hermes's ``task_links`` and ``task_comments`` tables from
``hermes_cli/kanban_db.py``. Korpha's existing ``KanbanCardRef``
captures **textual** ``#abc12345`` mentions in card bodies (PR
#207); ``KanbanCardLink`` here captures **structural** parent →
child dependencies that gate when a child becomes READY.

A parent link relationship means: child must wait for parent to
reach DONE (or have its review_evidence verdict=accepted) before
the dispatcher promotes the child past READY. This is the
``recompute_ready`` logic from Hermes's dispatcher — readiness is
dynamic, not a column state.

``KanbanCardComment`` is a first-class discussion thread on the
card. Today Korpha uses ``kanban_card_event.note`` for breadcrumbs
which conflates audit + discussion. Splitting them makes the
audit log canonical (column transitions only) and gives agents +
founders a real comments surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import primary_key_field, timestamp_field

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlmodel import Session


class KanbanCardLink(SQLModel, table=True):
    """Parent → child structural dependency between two kanban cards.

    Mirrors Hermes's ``task_links`` table. A parent link means the
    child can't enter IN_PROGRESS until the parent is DONE (or has
    review_verdict='accepted').

    Distinct from ``KanbanCardRef`` (PR #207) — refs are textual
    ``#abc12345`` mentions extracted from bodies; links are
    explicit dependency declarations.
    """

    __tablename__ = "kanban_card_link"

    id: UUID = primary_key_field()
    parent_card_id: UUID = Field(foreign_key="kanban_card.id", index=True)
    child_card_id: UUID = Field(foreign_key="kanban_card.id", index=True)
    business_id: UUID = Field(foreign_key="business.id", index=True)
    note: str | None = Field(
        default=None,
        description=(
            "Why this dep exists. e.g. 'KDP listing needs cover "
            "design from parent first'. Optional but useful in "
            "diagnostics."
        ),
    )
    created_at: datetime = timestamp_field()


class KanbanCardComment(SQLModel, table=True):
    """Discussion thread on a card. Open to agents + founders.

    Mirrors Hermes's ``task_comments`` table. Agents leave
    breadcrumbs ("found that Etsy requires JPEG, not PNG, retrying
    with conversion"); founders leave decisions ("approve the
    pivot to seasonal designs").
    """

    __tablename__ = "kanban_card_comment"

    id: UUID = primary_key_field()
    card_id: UUID = Field(foreign_key="kanban_card.id", index=True)
    business_id: UUID = Field(foreign_key="business.id", index=True)
    author_kind: str = Field(
        description=(
            "'agent' / 'founder' / 'system'. system is for "
            "dispatcher-generated notes (claim, reclaim, etc.)."
        ),
    )
    author_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id",
    )
    author_founder_id: UUID | None = Field(
        default=None, foreign_key="founder.id",
    )
    body: str
    created_at: datetime = timestamp_field(index=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def link_cards(
    session: "Session",
    *,
    parent_id: UUID,
    child_id: UUID,
    business_id: UUID,
    note: str | None = None,
) -> KanbanCardLink:
    """Create a parent → child dependency. Returns the existing
    link if one already exists (idempotent)."""
    from sqlmodel import select

    existing = list(session.exec(
        select(KanbanCardLink)
        .where(KanbanCardLink.parent_card_id == parent_id)
        .where(KanbanCardLink.child_card_id == child_id)
    ))
    if existing:
        return existing[0]
    link = KanbanCardLink(
        parent_card_id=parent_id,
        child_card_id=child_id,
        business_id=business_id,
        note=note,
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


def list_unmet_parents(
    session: "Session",
    *,
    child_id: UUID,
) -> list[UUID]:
    """Return the parent card IDs that block this child. Empty
    list = child is unblocked and can transition out of READY."""
    from sqlmodel import select

    from korpha.kanban.model import KanbanCard, KanbanColumn

    parent_ids = [
        link.parent_card_id for link in session.exec(
            select(KanbanCardLink)
            .where(KanbanCardLink.child_card_id == child_id)
        )
    ]
    if not parent_ids:
        return []
    blocked: list[UUID] = []
    for pid in parent_ids:
        parent = session.get(KanbanCard, pid)
        if parent is None:
            continue
        # Parent is "satisfied" if it's DONE, OR REVIEW with
        # verdict=accepted (the founder hasn't formally moved
        # it but it's verified).
        if parent.column == KanbanColumn.DONE:
            continue
        if (
            parent.column == KanbanColumn.REVIEW
            and parent.review_verdict == "accepted"
        ):
            continue
        blocked.append(pid)
    return blocked


def add_comment(
    session: "Session",
    *,
    card_id: UUID,
    business_id: UUID,
    body: str,
    author_kind: str = "agent",
    author_agent_role_id: UUID | None = None,
    author_founder_id: UUID | None = None,
) -> KanbanCardComment:
    """Append a comment to a card. Returns the persisted row."""
    body = (body or "").strip()
    if not body:
        raise ValueError("comment body cannot be empty")
    if author_kind not in {"agent", "founder", "system"}:
        raise ValueError(f"invalid author_kind: {author_kind!r}")
    c = KanbanCardComment(
        card_id=card_id,
        business_id=business_id,
        author_kind=author_kind,
        author_agent_role_id=author_agent_role_id,
        author_founder_id=author_founder_id,
        body=body,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def list_comments(
    session: "Session",
    *,
    card_id: UUID,
    limit: int = 100,
) -> list[KanbanCardComment]:
    """Return comments newest-first, capped at ``limit``."""
    from sqlmodel import select

    return list(session.exec(
        select(KanbanCardComment)
        .where(KanbanCardComment.card_id == card_id)
        .order_by(KanbanCardComment.created_at.desc())  # type: ignore[union-attr]
    ))[:limit]


__all__ = [
    "KanbanCardComment",
    "KanbanCardLink",
    "add_comment",
    "link_cards",
    "list_comments",
    "list_unmet_parents",
]
