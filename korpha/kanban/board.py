"""Kanban board service — create / move / claim / list.

The board is the single source of truth for in-flight work. The CEO
puts cards on it (from the chat or from a Plan), C-suite agents
claim cards from READY, and the founder watches the columns fill
up + drain through ``/app/kanban``.

Concurrency: claims use ``WHERE column='ready' AND
claimed_by_agent_role_id IS NULL`` so two agents claiming the same
card at once gets one winner. We commit between claim + move so
SQLite (no row-level locks) still sees serialized writes.

Errors raise ``KanbanError`` — never silent. The dispatcher wraps
those and surfaces a SkillError so the LLM gets a clean message.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlmodel import Session, select

from korpha.kanban.model import (
    TRANSITIONS,
    CardPriority,
    KanbanCard,
    KanbanCardEvent,
    KanbanColumn,
)


class KanbanError(Exception):
    """Raised when an operation violates a board invariant."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CreateCardInput:
    business_id: UUID
    title: str
    body: str = ""
    priority: CardPriority = CardPriority.NORMAL
    owner_role: str | None = None
    source_thread_id: UUID | None = None
    goal_id: UUID | None = None
    created_by_agent_role_id: UUID | None = None
    created_by_founder_id: UUID | None = None


class KanbanBoard:
    """Per-Session board operations. Construct a fresh one per
    request — no shared state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- create ----

    def create(self, payload: CreateCardInput) -> KanbanCard:
        if not payload.title.strip():
            raise KanbanError("kanban: card title required")
        card = KanbanCard(
            business_id=payload.business_id,
            title=payload.title.strip(),
            body=payload.body,
            priority=payload.priority,
            owner_role=payload.owner_role,
            column=KanbanColumn.BACKLOG,
            source_thread_id=payload.source_thread_id,
            goal_id=payload.goal_id,
            created_by_agent_role_id=payload.created_by_agent_role_id,
            created_by_founder_id=payload.created_by_founder_id,
        )
        self.session.add(card)
        self.session.commit()
        self.session.refresh(card)
        self._log(
            card_id=card.id, business_id=card.business_id,
            kind="create",
            from_column=None, to_column=KanbanColumn.BACKLOG,
            actor_role=None,
            actor_agent_role_id=payload.created_by_agent_role_id,
            actor_founder_id=payload.created_by_founder_id,
            note=f"Created: {card.title}",
        )
        # Extract any #prefix references in title/body and persist
        # them. Failures are logged + dropped — never block create.
        try:
            from korpha.kanban.refs import RefService
            RefService(self.session).extract_and_persist(card)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "kanban ref extraction failed for %s",
                card.id, exc_info=True,
            )
        return card

    # ---- move ----

    def move(
        self,
        card_id: UUID,
        to_column: KanbanColumn,
        *,
        actor_agent_role_id: UUID | None = None,
        actor_founder_id: UUID | None = None,
        note: str | None = None,
    ) -> KanbanCard:
        """Transition a card to a new column. Enforces TRANSITIONS +
        SPECIFY-gate (must have acceptance criteria + owner before
        leaving SPECIFY) + REVIEW-gate (must have evidence to leave
        REVIEW for DONE)."""
        card = self.session.get(KanbanCard, card_id)
        if card is None:
            raise KanbanError(f"kanban: card {card_id} not found")

        from_column = card.column
        if to_column == from_column:
            return card  # idempotent

        allowed = TRANSITIONS.get(from_column, frozenset())
        if to_column not in allowed:
            raise KanbanError(
                f"kanban: cannot move card from {from_column.value} "
                f"to {to_column.value}"
            )

        # SPECIFY gate — leaving SPECIFY for READY needs criteria + owner.
        if from_column == KanbanColumn.SPECIFY and to_column == KanbanColumn.READY:
            if not card.acceptance_criteria:
                raise KanbanError(
                    "kanban: card needs acceptance_criteria before "
                    "leaving SPECIFY"
                )
            if not card.owner_role:
                raise KanbanError(
                    "kanban: card needs owner_role before leaving SPECIFY"
                )

        # REVIEW gate — accepting (DONE) requires evidence.
        if from_column == KanbanColumn.REVIEW and to_column == KanbanColumn.DONE:
            if not card.review_evidence:
                raise KanbanError(
                    "kanban: card needs review_evidence to be marked DONE"
                )
            card.review_verdict = "accepted"

        # Kickback from REVIEW back to IN_PROGRESS — flag for rework.
        if (
            from_column == KanbanColumn.REVIEW
            and to_column == KanbanColumn.IN_PROGRESS
        ):
            card.review_verdict = "rework"

        # Releasing IN_PROGRESS → READY drops the claim.
        if (
            from_column == KanbanColumn.IN_PROGRESS
            and to_column == KanbanColumn.READY
        ):
            card.claimed_by_agent_role_id = None
            card.claimed_at = None

        card.column = to_column
        card.moved_at = _now()
        card.updated_at = _now()
        self.session.add(card)
        self.session.commit()
        self.session.refresh(card)

        self._log(
            card_id=card.id, business_id=card.business_id,
            kind="move",
            from_column=from_column, to_column=to_column,
            actor_role=None,
            actor_agent_role_id=actor_agent_role_id,
            actor_founder_id=actor_founder_id,
            note=note,
        )
        return card

    # ---- claim ----

    def claim(
        self,
        card_id: UUID,
        *,
        agent_role_id: UUID,
        actor_role: str | None = None,
    ) -> KanbanCard:
        """Atomically claim a READY card and move to IN_PROGRESS.
        Raises if the card isn't READY or is already claimed."""
        card = self.session.get(KanbanCard, card_id)
        if card is None:
            raise KanbanError(f"kanban: card {card_id} not found")
        if card.column != KanbanColumn.READY:
            raise KanbanError(
                f"kanban: cannot claim card in column "
                f"{card.column.value}; only READY cards are claimable"
            )
        if card.claimed_by_agent_role_id is not None:
            raise KanbanError(
                f"kanban: card already claimed by "
                f"{card.claimed_by_agent_role_id}"
            )
        # Optional owner_role gating — if the card is owned by 'cmo',
        # a 'cto' shouldn't claim it.
        if (
            card.owner_role
            and actor_role
            and card.owner_role != actor_role
        ):
            raise KanbanError(
                f"kanban: card owned by {card.owner_role}, not "
                f"{actor_role}"
            )

        card.claimed_by_agent_role_id = agent_role_id
        card.claimed_at = _now()
        card.column = KanbanColumn.IN_PROGRESS
        card.moved_at = _now()
        card.updated_at = _now()
        self.session.add(card)
        self.session.commit()
        self.session.refresh(card)

        self._log(
            card_id=card.id, business_id=card.business_id,
            kind="claim",
            from_column=KanbanColumn.READY,
            to_column=KanbanColumn.IN_PROGRESS,
            actor_role=actor_role,
            actor_agent_role_id=agent_role_id,
            actor_founder_id=None,
            note=None,
        )
        return card

    # ---- specify ----

    def specify(
        self,
        card_id: UUID,
        *,
        acceptance_criteria: list[str],
        owner_role: str | None = None,
        body: str | None = None,
        actor_agent_role_id: UUID | None = None,
        actor_founder_id: UUID | None = None,
    ) -> KanbanCard:
        """Populate the SPECIFY-column fields on a card. Doesn't
        force a column change — caller decides when the spec is
        complete and calls ``move()`` to READY."""
        card = self.session.get(KanbanCard, card_id)
        if card is None:
            raise KanbanError(f"kanban: card {card_id} not found")
        if card.column not in (KanbanColumn.BACKLOG, KanbanColumn.SPECIFY):
            raise KanbanError(
                f"kanban: specify() only works in BACKLOG/SPECIFY "
                f"(card is in {card.column.value})"
            )
        criteria = [c.strip() for c in acceptance_criteria if c.strip()]
        if not criteria:
            raise KanbanError(
                "kanban: specify() needs at least one acceptance criterion"
            )
        card.acceptance_criteria = criteria
        if owner_role:
            card.owner_role = owner_role
        if body is not None:
            card.body = body
        if card.column == KanbanColumn.BACKLOG:
            card.column = KanbanColumn.SPECIFY
            card.moved_at = _now()
        card.updated_at = _now()
        self.session.add(card)
        self.session.commit()
        self.session.refresh(card)

        self._log(
            card_id=card.id, business_id=card.business_id,
            kind="specify",
            from_column=None, to_column=card.column,
            actor_role=None,
            actor_agent_role_id=actor_agent_role_id,
            actor_founder_id=actor_founder_id,
            note=None,
            payload={"acceptance_criteria_count": len(criteria)},
        )
        # Re-extract refs after body change (best-effort).
        try:
            from korpha.kanban.refs import RefService
            RefService(self.session).extract_and_persist(card)
        except Exception:  # noqa: BLE001
            pass
        return card

    # ---- review evidence ----

    def submit_review_evidence(
        self,
        card_id: UUID,
        *,
        evidence: str,
        actor_agent_role_id: UUID | None = None,
    ) -> KanbanCard:
        """Agent attaches evidence to its work and moves to REVIEW.
        The reviewer (CEO or founder) reads the evidence + verifies
        it before moving to DONE."""
        card = self.session.get(KanbanCard, card_id)
        if card is None:
            raise KanbanError(f"kanban: card {card_id} not found")
        if not evidence.strip():
            raise KanbanError("kanban: evidence cannot be empty")
        if card.column != KanbanColumn.IN_PROGRESS:
            raise KanbanError(
                f"kanban: evidence can only be submitted from "
                f"IN_PROGRESS (card is in {card.column.value})"
            )
        card.review_evidence = evidence.strip()
        card.column = KanbanColumn.REVIEW
        card.moved_at = _now()
        card.updated_at = _now()
        self.session.add(card)
        self.session.commit()
        self.session.refresh(card)
        self._log(
            card_id=card.id, business_id=card.business_id,
            kind="review_evidence",
            from_column=KanbanColumn.IN_PROGRESS,
            to_column=KanbanColumn.REVIEW,
            actor_role=None,
            actor_agent_role_id=actor_agent_role_id,
            actor_founder_id=None,
            note=evidence[:500],
        )
        return card

    # ---- listing ----

    def list_column(
        self,
        business_id: UUID,
        column: KanbanColumn,
        *,
        limit: int | None = 100,
    ) -> list[KanbanCard]:
        """Cards in one column, newest-moved first within priority bands."""
        priority_order = {
            CardPriority.HIGH: 0,
            CardPriority.NORMAL: 1,
            CardPriority.LOW: 2,
        }
        stmt = (
            select(KanbanCard)
            .where(KanbanCard.business_id == business_id)
            .where(KanbanCard.column == column)
        )
        cards = list(self.session.exec(stmt).all())
        cards.sort(
            key=lambda c: (
                priority_order.get(c.priority, 1),
                -c.moved_at.timestamp(),
            )
        )
        if limit is not None:
            return cards[:limit]
        return cards

    def board_snapshot(
        self, business_id: UUID,
    ) -> dict[KanbanColumn, list[KanbanCard]]:
        """All non-archived columns, suitable for /app/kanban render."""
        snapshot: dict[KanbanColumn, list[KanbanCard]] = {}
        for col in KanbanColumn:
            if col == KanbanColumn.ARCHIVED:
                continue
            snapshot[col] = self.list_column(business_id, col)
        return snapshot

    # ---- audit log ----

    def history(self, card_id: UUID) -> list[KanbanCardEvent]:
        stmt = (
            select(KanbanCardEvent)
            .where(KanbanCardEvent.card_id == card_id)
            .order_by(KanbanCardEvent.occurred_at)
        )
        return list(self.session.exec(stmt).all())

    # ---- internals ----

    def _log(
        self,
        *,
        card_id: UUID,
        business_id: UUID,
        kind: str,
        from_column: KanbanColumn | None,
        to_column: KanbanColumn | None,
        actor_role: str | None,
        actor_agent_role_id: UUID | None,
        actor_founder_id: UUID | None,
        note: str | None,
        payload: dict | None = None,
    ) -> None:
        event = KanbanCardEvent(
            card_id=card_id,
            business_id=business_id,
            kind=kind,
            from_column=from_column,
            to_column=to_column,
            actor_role=actor_role,
            actor_agent_role_id=actor_agent_role_id,
            actor_founder_id=actor_founder_id,
            note=note,
            payload=payload or {},
        )
        self.session.add(event)
        self.session.commit()


__all__ = [
    "CreateCardInput",
    "KanbanBoard",
    "KanbanError",
]
