"""Cross-card reference extractor.

Cards reference each other in prose: "depends on the auth card",
"unblocks the pricing-page card", "see the Stripe webhook card
for the integration shape." Without structure those mentions
are invisible to the dashboard — Mike has to scroll the board
to find what's connected.

This module extracts ``#<prefix>`` mentions from card title +
body and persists them as ``KanbanCardRef`` rows. The dashboard
+ weekly digest read them to render "this card unblocked X+Y"
links.

Convention: ``#<8+ char UUID prefix>`` → reference to a card.
We resolve to a real card_id at extraction time so the link is
durable. Prefixes shorter than 8 chars are too ambiguous —
ignored. Multi-match prefixes (rare; UUID collisions unlikely
across a single business) are also ignored to keep the graph
clean.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional
from uuid import UUID

from sqlmodel import Field, Session, SQLModel, select

from korpha.db._base import primary_key_field, timestamp_field
from korpha.kanban.model import KanbanCard


_REF_RE = re.compile(r"#([0-9a-fA-F]{8,32})\b")
"""``#abcdef12`` style references. 8 chars is enough for
disambiguation in a single business; capping at 32 avoids
matching long hashes that aren't UUIDs."""


class RefRelation(StrEnum):
    """How are these two cards related? Today we infer from
    surrounding text — "depends on" / "unblocks" / "see also".
    Default is GENERIC; semantics layer can grow over time."""

    GENERIC = "generic"
    DEPENDS_ON = "depends_on"
    UNBLOCKS = "unblocks"
    SEE_ALSO = "see_also"


class KanbanCardRef(SQLModel, table=True):
    """Directed edge between two kanban cards. The ``source`` card
    contains the ``#prefix`` mention pointing to ``target``."""

    __tablename__ = "kanban_card_ref"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    source_card_id: UUID = Field(
        foreign_key="kanban_card.id", index=True,
    )
    target_card_id: UUID = Field(
        foreign_key="kanban_card.id", index=True,
    )

    relation: RefRelation = Field(
        default=RefRelation.GENERIC, index=True,
    )

    matched_text: str = Field(
        default="",
        description=(
            "The substring around the match — used to display "
            "context in the dashboard. e.g. 'depends on #abc123 "
            "for the auth model'."
        ),
    )

    created_at: datetime = timestamp_field()


_RELATION_HINTS: tuple[tuple[str, RefRelation], ...] = (
    ("depends on", RefRelation.DEPENDS_ON),
    ("blocked by", RefRelation.DEPENDS_ON),
    ("requires", RefRelation.DEPENDS_ON),
    ("unblocks", RefRelation.UNBLOCKS),
    ("enables", RefRelation.UNBLOCKS),
    ("see also", RefRelation.SEE_ALSO),
    ("related to", RefRelation.SEE_ALSO),
    ("see ", RefRelation.SEE_ALSO),
)


def _classify_relation(snippet: str) -> RefRelation:
    """Look at a window around the match for a relation hint."""
    lower = snippet.lower()
    for phrase, rel in _RELATION_HINTS:
        if phrase in lower:
            return rel
    return RefRelation.GENERIC


@dataclass
class RefService:
    """Per-Session ops for the cross-card reference graph."""

    session: Session

    def extract_and_persist(
        self,
        card: KanbanCard,
    ) -> list[KanbanCardRef]:
        """Re-scan ``card.title + body`` for ``#prefix`` mentions
        and persist them as KanbanCardRef rows.

        Idempotent: clears any existing refs FROM this card and
        rewrites them. Safe to call after every card edit."""
        # Drop existing refs from this source so a removed mention
        # doesn't leave a stale edge.
        existing = list(self.session.exec(
            select(KanbanCardRef)
            .where(KanbanCardRef.source_card_id == card.id)
        ).all())
        for old in existing:
            self.session.delete(old)
        if existing:
            self.session.commit()

        text = " ".join(filter(None, [card.title, card.body]))
        refs: list[KanbanCardRef] = []
        seen: set[tuple[UUID, RefRelation]] = set()
        for match in _REF_RE.finditer(text):
            prefix = match.group(1).lower()
            target = self._resolve_prefix(card.business_id, prefix)
            if target is None or target.id == card.id:
                continue
            # Snippet window around the match for relation hint
            start = max(0, match.start() - 32)
            end = min(len(text), match.end() + 16)
            snippet = text[start:end]
            relation = _classify_relation(snippet)
            key = (target.id, relation)
            if key in seen:
                continue
            ref = KanbanCardRef(
                business_id=card.business_id,
                source_card_id=card.id,
                target_card_id=target.id,
                relation=relation,
                matched_text=snippet.strip(),
            )
            self.session.add(ref)
            refs.append(ref)
            seen.add(key)
        if refs:
            self.session.commit()
            for ref in refs:
                self.session.refresh(ref)
        return refs

    def references_from(
        self, card_id: UUID,
    ) -> list[KanbanCardRef]:
        """Outgoing edges — cards this one mentions."""
        return list(self.session.exec(
            select(KanbanCardRef)
            .where(KanbanCardRef.source_card_id == card_id)
        ).all())

    def references_to(
        self, card_id: UUID,
    ) -> list[KanbanCardRef]:
        """Incoming edges — cards that mention this one."""
        return list(self.session.exec(
            select(KanbanCardRef)
            .where(KanbanCardRef.target_card_id == card_id)
        ).all())

    def _resolve_prefix(
        self, business_id: UUID, prefix: str,
    ) -> Optional[KanbanCard]:
        """Match a UUID prefix against this business's cards.
        Returns None on zero or multi matches — we keep the
        graph clean by ignoring ambiguous prefixes."""
        cards = list(self.session.exec(
            select(KanbanCard)
            .where(KanbanCard.business_id == business_id)
        ).all())
        matches = [
            c for c in cards if str(c.id).lower().startswith(prefix)
        ]
        if len(matches) == 1:
            return matches[0]
        return None


__all__ = [
    "KanbanCardRef",
    "RefRelation",
    "RefService",
]
