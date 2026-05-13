"""Kanban board for the C-suite.

Mike sees one shared board with cards moving through columns. The C-suite
agents pick from the board, work the card, and move it to ``review``.
The CEO (or Mike) accepts → ``done`` or kicks it back → ``backlog``.

Why a board (not just the existing Approvals queue):

  * Approvals are transactional — one ask, one yes/no. The board lets
    Mike see the *compounding* state of work over weeks.
  * Multiple agents can pick from one queue without stepping on each
    other (the dispatcher claims a card with row-level locking).
  * The ``specify`` column gates execution — a card can't move to
    ``in_progress`` until it has acceptance criteria, scoping the
    LLM's work and reducing hallucinated tangents.
  * The ``review`` column is the hallucination gate — a different
    role-typed agent (or Mike) inspects the result before it lands
    in ``done``.

Adapted from the Hermes v0.13 kanban-multi-agent feature; we keep the
column shape but bind cards to ``business_id`` (not a workspace) and
let the CEO own dispatch instead of a separate orchestrator process.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import (
    json_column, primary_key_field, timestamp_field,
)


class KanbanColumn(StrEnum):
    """Lifecycle columns. Cards move left → right.

    BACKLOG    — captured idea, not yet specified
    SPECIFY    — being scoped: acceptance criteria, owner, estimate
    READY      — fully specified, awaiting an agent claim
    IN_PROGRESS — claimed by an agent, work underway
    REVIEW     — agent done, awaiting verification (hallucination gate)
    DONE       — verified + accepted
    BLOCKED    — needs founder input, parked off the main flow
    ARCHIVED   — soft-deleted (kept for audit / search)
    """

    BACKLOG = "backlog"
    SPECIFY = "specify"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"
    ARCHIVED = "archived"


# Allowed transitions. The set is intentionally small; the board's
# value is *enforced flow*, not arbitrary moves. A card moves
# backward only via explicit reopen (DONE → BACKLOG) or kickback
# (REVIEW → IN_PROGRESS / BACKLOG).
TRANSITIONS: dict[KanbanColumn, frozenset[KanbanColumn]] = {
    KanbanColumn.BACKLOG: frozenset({
        KanbanColumn.SPECIFY,
        KanbanColumn.ARCHIVED,
        KanbanColumn.BLOCKED,
    }),
    KanbanColumn.SPECIFY: frozenset({
        KanbanColumn.READY,
        KanbanColumn.BACKLOG,
        KanbanColumn.BLOCKED,
        KanbanColumn.ARCHIVED,
    }),
    KanbanColumn.READY: frozenset({
        KanbanColumn.IN_PROGRESS,
        KanbanColumn.BACKLOG,
        KanbanColumn.BLOCKED,
        KanbanColumn.ARCHIVED,
    }),
    KanbanColumn.IN_PROGRESS: frozenset({
        KanbanColumn.REVIEW,
        KanbanColumn.BLOCKED,
        KanbanColumn.READY,  # release claim
        KanbanColumn.ARCHIVED,
    }),
    KanbanColumn.REVIEW: frozenset({
        KanbanColumn.DONE,
        KanbanColumn.IN_PROGRESS,  # kickback for rework
        KanbanColumn.BACKLOG,      # reject + re-spec
        KanbanColumn.ARCHIVED,
    }),
    KanbanColumn.BLOCKED: frozenset({
        KanbanColumn.SPECIFY,
        KanbanColumn.READY,
        KanbanColumn.IN_PROGRESS,
        KanbanColumn.BACKLOG,
        KanbanColumn.ARCHIVED,
    }),
    KanbanColumn.DONE: frozenset({
        KanbanColumn.BACKLOG,    # reopen
        KanbanColumn.ARCHIVED,
    }),
    KanbanColumn.ARCHIVED: frozenset({
        KanbanColumn.BACKLOG,    # un-archive
    }),
}


class CardPriority(StrEnum):
    """Coarse priority. Mike rarely tunes this beyond high/normal —
    fancy weighting goes unused. The board sorts within column by
    (priority desc, updated_at desc)."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class KanbanCard(SQLModel, table=True):
    """One unit of work on the board. Lives until archived."""

    __tablename__ = "kanban_card"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    # Org-tree scope (PR3 — recursive BusinessUnit). Nullable during
    # backfill; future migration tightens to non-null once every row is
    # confirmed to reference a unit.
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )

    title: str = Field(
        description=(
            "Short imperative — 'launch landing page', 'write 3 LinkedIn "
            "posts'. Shows in column views."
        ),
    )
    body: str = Field(
        default="",
        description=(
            "Longer description, links, raw founder words. Optional "
            "while in BACKLOG; required to leave SPECIFY."
        ),
    )

    column: KanbanColumn = Field(
        default=KanbanColumn.BACKLOG, index=True,
    )
    priority: CardPriority = Field(
        default=CardPriority.NORMAL, index=True,
    )

    # Acceptance criteria — populated during SPECIFY. The board
    # refuses the SPECIFY → READY move while this is empty.
    acceptance_criteria: list[str] = Field(
        default_factory=list, sa_column=json_column(),
    )

    # Owner — which role the card is assigned to. Null until SPECIFY
    # picks one. Ties dispatch to a single agent so two CTOs don't
    # double-dip on the same card.
    owner_role: str | None = Field(default=None, index=True)
    """RoleType value as a string ('cto' / 'cmo' / 'coo'). Plain
    string instead of a FK so we can survive role-name changes."""

    # Claim tracking — the agent currently working the card.
    claimed_by_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id", index=True,
    )
    claimed_at: datetime | None = Field(default=None)

    # Provenance — who created the card.
    created_by_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id",
    )
    created_by_founder_id: UUID | None = Field(
        default=None, foreign_key="founder.id",
    )

    # Linkage — optional pointers back into the rest of the system.
    source_thread_id: UUID | None = Field(
        default=None, foreign_key="thread.id", index=True,
        description="The chat thread this card was born in.",
    )
    approval_id: UUID | None = Field(
        default=None,
        description=(
            "If the card's execution requires founder approval, the "
            "Approval row staged for it. Null otherwise."
        ),
    )
    goal_id: UUID | None = Field(
        default=None,
        description=(
            "If this card was generated as a sub-task of an active "
            "Goal (Ralph loop), the Goal id. Lets us roll up progress "
            "to the goal level."
        ),
    )

    # Hallucination gate — populated when the card moves to REVIEW.
    review_evidence: str | None = Field(
        default=None,
        description=(
            "What the agent says it produced — URL of the deployed "
            "page, message id of the sent post, file path of the "
            "written copy. The reviewer (CEO or Mike) verifies this "
            "matches reality before accepting."
        ),
    )
    review_verdict: str | None = Field(
        default=None,
        description=(
            "'accepted' / 'rework' / null. Set by reviewer when "
            "moving REVIEW → DONE or REVIEW → IN_PROGRESS."
        ),
    )

    # Free-form notes / metadata bag for plugins to annotate.
    metadata_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )

    created_at: datetime = timestamp_field(index=True)
    updated_at: datetime = timestamp_field()
    moved_at: datetime = timestamp_field(index=True)
    """Last column transition. Sorts within-column views by recency."""


class KanbanCardEvent(SQLModel, table=True):
    """Append-only audit log of column transitions + claims. Lets us
    answer 'how long did this card spend in REVIEW' without scanning
    chat history."""

    __tablename__ = "kanban_card_event"

    id: UUID = primary_key_field()
    card_id: UUID = Field(foreign_key="kanban_card.id", index=True)
    business_id: UUID = Field(foreign_key="business.id", index=True)

    kind: str = Field(
        index=True,
        description=(
            "'move' (column change) / 'claim' / 'release' / "
            "'comment' / 'review_evidence' / 'review_verdict'."
        ),
    )
    from_column: KanbanColumn | None = Field(default=None)
    to_column: KanbanColumn | None = Field(default=None)
    actor_role: str | None = Field(default=None, index=True)
    actor_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id",
    )
    actor_founder_id: UUID | None = Field(
        default=None, foreign_key="founder.id",
    )

    note: str | None = Field(default=None)
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )

    occurred_at: datetime = timestamp_field(index=True)


__all__ = [
    "TRANSITIONS",
    "CardPriority",
    "KanbanCard",
    "KanbanCardEvent",
    "KanbanColumn",
]
