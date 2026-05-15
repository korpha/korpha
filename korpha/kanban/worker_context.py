"""Bounded structured prompt for a worker about to attempt a card.

Port of Hermes's ``hermes_cli/kanban_db.build_worker_context`` —
adapted to Korpha's SQLModel schema (no ``runs`` table, no
``task_comments`` table yet, but we have ``kanban_card_event`` for
audit + ``Activity`` rows).

The returned text is a structured Markdown block:

  # Kanban card <id>: <title>
  Status / Unit / Claimed-by

  ## Body / Spec
    body
    acceptance_criteria

  ## Prior attempts on this card
    most recent N attempts, capped at K bytes each

  ## Parent-card results
    body / review_evidence of parent cards (via kanban_card_ref)

  ## Recent activity on this card
    last M kanban_card_event rows

Every section is bounded by char caps so the prompt stays ≤100k
even on pathological boards. Per-card cap floors match Hermes's
``_CTX_MAX_*`` constants.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# Match Hermes's caps so behavior is comparable across systems.
_CTX_MAX_PRIOR_ATTEMPTS = 10
_CTX_MAX_EVENTS = 20
_CTX_MAX_PARENTS = 10
_CTX_MAX_FIELD_BYTES = 4 * 1024     # 4 KB per summary/evidence/note
_CTX_MAX_BODY_BYTES = 8 * 1024       # 8 KB per card body
_CTX_MAX_CRITERIA_BYTES = 2 * 1024   # 2 KB per acceptance criterion


def _cap(s: str | None, limit: int = _CTX_MAX_FIELD_BYTES) -> str:
    """Truncate to ``limit`` chars with a visible ellipsis. Hermes's
    ``_cap`` helper, same shape."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"


def build_worker_context(
    *,
    engine: "Engine",
    card_id: UUID | str,
) -> str:
    """Return the full text a worker should read to understand its
    card. Bounded by the ``_CTX_MAX_*`` caps above so the prompt
    stays predictable even on retry-heavy / comment-heavy boards.

    Returns an empty string on lookup failure rather than raising —
    workers should be able to attempt even when context lookup is
    flaky. Worker prompt callers should always provide a fallback.
    """
    from sqlmodel import Session, select

    from korpha.business_units.model import BusinessUnit
    from korpha.cofounder.model import AgentRole
    from korpha.kanban.model import (
        KanbanCard,
        KanbanCardEvent,
    )

    try:
        cid = card_id if isinstance(card_id, UUID) else UUID(str(card_id))
    except (ValueError, TypeError):
        return ""

    with Session(engine) as session:
        card = session.get(KanbanCard, cid)
        if card is None:
            return ""

        lines: list[str] = []
        lines.append(
            f"# Kanban card `{str(card.id)[:8]}`: {card.title}"
        )
        lines.append("")
        col = (
            card.column.value if hasattr(card.column, "value")
            else str(card.column)
        )
        prio = (
            card.priority.value if hasattr(card.priority, "value")
            else str(card.priority)
        )
        lines.append(f"Status: **{col}** · Priority: {prio}")
        if card.owner_role:
            lines.append(f"Owner role: `{card.owner_role}`")
        if card.business_unit_id:
            unit = session.get(BusinessUnit, card.business_unit_id)
            if unit is not None:
                lines.append(
                    f"Business unit: `{unit.name}` "
                    f"({unit.kind.value if hasattr(unit.kind, 'value') else unit.kind})"
                )
        if card.claimed_by_agent_role_id:
            claimer = session.get(AgentRole, card.claimed_by_agent_role_id)
            if claimer is not None:
                lines.append(
                    f"Claimed by: `{claimer.title}` "
                    f"({claimer.role_type})"
                )
        lines.append("")

        # ----- Body + acceptance criteria -----
        if card.body and card.body.strip():
            lines.append("## Body")
            lines.append(_cap(card.body, _CTX_MAX_BODY_BYTES))
            lines.append("")
        if card.acceptance_criteria:
            lines.append("## Acceptance criteria")
            for i, c in enumerate(card.acceptance_criteria, 1):
                lines.append(f"{i}. {_cap(c, _CTX_MAX_CRITERIA_BYTES)}")
            lines.append("")

        # ----- Prior attempts on THIS card -----
        # We don't have a 'runs' table; the closest substitute is
        # the kanban_card_event log, filtered to attempt-related
        # entries (claim, move_to_review, review_verdict=rework).
        attempt_events = list(session.exec(
            select(KanbanCardEvent)
            .where(KanbanCardEvent.card_id == card.id)
            .where(
                KanbanCardEvent.kind.in_(  # type: ignore[union-attr]
                    ["claim", "submit_evidence", "move"]
                )
            )
            .order_by(KanbanCardEvent.occurred_at.desc())  # type: ignore[union-attr]
        ))[:_CTX_MAX_PRIOR_ATTEMPTS]
        if attempt_events:
            lines.append("## Prior attempts on this card")
            for ev in reversed(attempt_events):  # chronological
                when = ev.occurred_at.isoformat() if ev.occurred_at else "?"
                note = (ev.note or "").strip()
                trans = ""
                if ev.from_column is not None and ev.to_column is not None:
                    trans = (
                        f" ({ev.from_column.value if hasattr(ev.from_column, 'value') else ev.from_column}"
                        f" → {ev.to_column.value if hasattr(ev.to_column, 'value') else ev.to_column})"
                    )
                lines.append(
                    f"- [{when}] {ev.kind}{trans}: "
                    f"{_cap(note, _CTX_MAX_FIELD_BYTES) or '(no note)'}"
                )
            lines.append("")

        # ----- Parent-card results (via cross-card refs) -----
        # Look for #-references in body/title pointing at other cards
        # (PR #207 model). Pull their review_evidence so this worker
        # can see what the prerequisite delivered.
        try:
            from korpha.kanban.model import KanbanCardRef

            refs = list(session.exec(
                select(KanbanCardRef)
                .where(KanbanCardRef.source_card_id == card.id)
                .order_by(KanbanCardRef.created_at)  # type: ignore[union-attr]
            ))[:_CTX_MAX_PARENTS]
        except ImportError:
            refs = []
        if refs:
            lines.append("## Referenced cards (parents/dependencies)")
            for ref in refs:
                target = session.get(KanbanCard, ref.target_card_id)
                if target is None:
                    continue
                t_col = (
                    target.column.value if hasattr(target.column, "value")
                    else str(target.column)
                )
                lines.append(
                    f"### `{str(target.id)[:8]}` ({t_col}) — {target.title}"
                )
                if target.review_evidence:
                    lines.append("Evidence:")
                    lines.append(
                        _cap(target.review_evidence, _CTX_MAX_FIELD_BYTES)
                    )
                elif target.body:
                    lines.append("Body:")
                    lines.append(_cap(target.body, _CTX_MAX_BODY_BYTES))
                lines.append("")

        # ----- Recent events on this card (capped) -----
        recent_events = list(session.exec(
            select(KanbanCardEvent)
            .where(KanbanCardEvent.card_id == card.id)
            .order_by(KanbanCardEvent.occurred_at.desc())  # type: ignore[union-attr]
        ))[:_CTX_MAX_EVENTS]
        if recent_events:
            lines.append("## Recent activity")
            for ev in reversed(recent_events):
                when = ev.occurred_at.isoformat() if ev.occurred_at else "?"
                lines.append(
                    f"- [{when}] {ev.kind}: "
                    f"{_cap(ev.note, _CTX_MAX_FIELD_BYTES) or '(no note)'}"
                )
            lines.append("")

        return "\n".join(lines).strip()


__all__ = ["build_worker_context"]
