"""Recent-business-output preamble for Director / Worker prompts.

Problem this fixes: parallel Director turns don't see each other's
output. The CMO drafting "KDP listing copy for both books" blocked on
"I don't have the titles or descriptions" — at the same moment another
CMO turn shipped *"Draw Everything: A 30-Day Guide for Absolute
Beginners"* as the title for one of those books. Same-turn race.

Next-turn fix (this module): inject a snapshot of recent shipped work
into every Director system prompt. After the parallel race completes,
the next "go" turn's Directors see what siblings produced and can pull
from it instead of re-asking.

Doesn't fix same-turn races (would need serialization by owner_role,
which is a bigger refactor). But it does fix the much more common case
where work is dispatched across multiple turns and each Director
forgets what the team has shipped 5 minutes ago.

Keep this preamble bounded — every Director call pays prompt tax.
Cap at the 5 most recent REVIEW evidences + most recent 10 comments,
each truncated to ~400 chars.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlmodel import Session

logger = logging.getLogger(__name__)

_MAX_REVIEW_ITEMS = 5
_MAX_COMMENT_ITEMS = 10
_REVIEW_EVIDENCE_TRUNC = 400
_COMMENT_TRUNC = 300


def build_recent_business_output_block(
    session: "Session",
    *,
    business_id: UUID,
    exclude_card_id: UUID | None = None,
) -> str:
    """Return a markdown block summarizing what the team has shipped /
    discussed recently. Empty string if nothing useful to report.

    ``exclude_card_id`` filters out the card the Director is *currently*
    working on — that card's own evidence is in scope via the assignment,
    not by reference.
    """
    try:
        from sqlmodel import select
        from korpha.kanban.model import KanbanCard, KanbanColumn
        from korpha.kanban.relations import KanbanCardComment
    except Exception:  # noqa: BLE001
        return ""

    parts: list[str] = []

    # Recent REVIEW evidence — what the team has actually shipped.
    try:
        stmt = (
            select(KanbanCard)
            .where(KanbanCard.business_id == business_id)
            .where(KanbanCard.column == KanbanColumn.REVIEW)
            .where(KanbanCard.review_evidence.is_not(None))  # type: ignore[union-attr]
        )
        if exclude_card_id is not None:
            stmt = stmt.where(KanbanCard.id != exclude_card_id)
        rows = list(session.exec(stmt).all())
        rows.sort(
            key=lambda c: (
                c.moved_at.timestamp() if c.moved_at else 0
            ),
            reverse=True,
        )
        rows = rows[:_MAX_REVIEW_ITEMS]
    except Exception:  # noqa: BLE001
        rows = []

    if rows:
        lines = [
            "Recent shipped work the team has produced "
            "(authoritative — pull titles/copy/specs from here "
            "instead of asking the Founder to provide them):",
        ]
        for c in rows:
            evidence = (c.review_evidence or "").strip()
            if not evidence:
                continue
            trunc = evidence[:_REVIEW_EVIDENCE_TRUNC]
            if len(evidence) > _REVIEW_EVIDENCE_TRUNC:
                trunc += "…"
            owner = f" [{c.owner_role.upper()}]" if c.owner_role else ""
            lines.append(f"- **{c.title}**{owner}: {trunc}")
        parts.append("\n".join(lines))

    # Recent founder-authored comments — direct notes / unblocks /
    # decisions Mike has left on cards. Always-on context (no card
    # exclude — even the current card's comments help).
    try:
        stmt = (
            select(KanbanCardComment)
            .where(KanbanCardComment.business_id == business_id)
            .where(KanbanCardComment.author_kind == "founder")
            .order_by(KanbanCardComment.created_at.desc())  # type: ignore[union-attr]
        )
        comments = list(session.exec(stmt).all())[:_MAX_COMMENT_ITEMS]
    except Exception:  # noqa: BLE001
        comments = []

    if comments:
        lines = [
            "Recent Founder notes on cards (binding context — apply "
            "these without asking again):",
        ]
        for cm in comments:
            body = (cm.body or "").strip()
            trunc = body[:_COMMENT_TRUNC]
            if len(body) > _COMMENT_TRUNC:
                trunc += "…"
            lines.append(f"- {trunc}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


__all__ = ["build_recent_business_output_block"]
