"""Bounded founder/agent notes — Hermes-style ``MEMORY.md`` /
``USER.md`` parity.

The cofounder's actual self-improvement loop. Two bounded text
stores per ``(business_id, founder_id)`` — *memory* (the agent's
personal notes about the project + lessons it learned) and *user*
(the founder's profile + preferences) — that get **auto-injected
into every CEO/Director system prompt** so the agent carries
forward what it knows without anyone telling it to recall.

Char limits force consolidation. When a store hits its cap, the
caller is told "memory at 2,100/2,200 — replace or remove
existing entries first" and the agent merges related entries
before adding new ones. That tension is what produces high-
density entries instead of a sprawling diary.

Why not just dump the whole long-term-memory table into the
prompt? Because:

  1. Token budget. 2,200 chars (~800 tokens) is a meaningful
     prompt prefix. The full memory table can be megabytes.
  2. Salience. The bounded view is a *curated* summary the agent
     itself maintains. The big table is a search index.
  3. Frozen-prefix caching. The injection is captured once at
     session start and never mutates mid-session, so the LLM
     prefix cache stays warm.

This module is the bounded text layer. The existing
``LongTermMemory`` ABC + DB store stays for free-form recall
that doesn't fit in the prompt budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from sqlmodel import Field, Session, SQLModel, select

from korpha.db._base import primary_key_field, timestamp_field


NoteStoreName = Literal["memory", "user"]


@dataclass(frozen=True)
class NoteStoreSpec:
    """Per-store config: char limit + display labels."""

    name: NoteStoreName
    char_limit: int
    header: str
    """Shown to the agent as the section title in the system prompt."""

    blurb: str
    """Short hint — what to put in this store vs the other one."""


MEMORY_STORE = NoteStoreSpec(
    name="memory",
    char_limit=2200,
    header="AGENT MEMORY (your personal notes)",
    blurb=(
        "Project facts, conventions discovered, lessons learned, "
        "things that worked, things that didn't. NOT user "
        "preferences — those go in USER PROFILE."
    ),
)
USER_STORE = NoteStoreSpec(
    name="user",
    char_limit=1375,
    header="USER PROFILE (the founder's preferences + identity)",
    blurb=(
        "Mike's communication style, role, time budget, tools he "
        "knows, things he hates. Pure facts about the human."
    ),
)
STORES: dict[NoteStoreName, NoteStoreSpec] = {
    "memory": MEMORY_STORE,
    "user": USER_STORE,
}


class FounderNote(SQLModel, table=True):
    """One bounded note. Either ``store='memory'`` (agent's own
    notes about the project) or ``store='user'`` (founder profile)."""

    __tablename__ = "founder_note"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    founder_id: UUID = Field(foreign_key="founder.id", index=True)
    store: str = Field(
        index=True,
        description=(
            "'memory' (agent notes) or 'user' (founder profile). "
            "Stored as a string rather than enum so plugins can "
            "introduce custom stores without a schema migration."
        ),
    )
    content: str = Field(
        description=(
            "The note body. Free-form text. "
            "Multi-line allowed; the renderer joins entries with "
            "the section delimiter."
        ),
    )
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


class NoteCapacityError(Exception):
    """Raised when ``add`` would exceed the store's char limit.
    Carries usage info so callers can decide how to consolidate."""

    def __init__(
        self, *,
        store: NoteStoreName,
        current_chars: int,
        attempted_chars: int,
        limit: int,
    ) -> None:
        self.store = store
        self.current_chars = current_chars
        self.attempted_chars = attempted_chars
        self.limit = limit
        super().__init__(
            f"founder_note: {store} at "
            f"{current_chars}/{limit} chars; adding "
            f"{attempted_chars} more would exceed. Replace or "
            "remove existing entries first."
        )


class NoteNotFound(Exception):
    """``replace`` / ``remove`` couldn't find a unique substring match."""


_DELIM = "§"
"""Section delimiter shown in the rendered prompt block. Matches the
Hermes convention so prompts read consistently across the two systems."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FounderNoteService:
    """Per-Session note operations. Construct one per request."""

    session: Session

    def list(
        self, *,
        business_id: UUID,
        founder_id: UUID,
        store: NoteStoreName,
    ) -> list[FounderNote]:
        if store not in STORES:
            raise ValueError(f"unknown store {store!r}")
        rows = list(self.session.exec(
            select(FounderNote)
            .where(FounderNote.business_id == business_id)
            .where(FounderNote.founder_id == founder_id)
            .where(FounderNote.store == store)
            .order_by(FounderNote.created_at)  # type: ignore[arg-type]
        ).all())
        return rows

    def total_chars(
        self, *,
        business_id: UUID,
        founder_id: UUID,
        store: NoteStoreName,
    ) -> int:
        return sum(len(n.content) for n in self.list(
            business_id=business_id, founder_id=founder_id, store=store,
        ))

    # ---- mutations ----

    def add(
        self, *,
        business_id: UUID,
        founder_id: UUID,
        store: NoteStoreName,
        content: str,
    ) -> FounderNote:
        spec = STORES.get(store)
        if spec is None:
            raise ValueError(f"unknown store {store!r}")
        text = (content or "").strip()
        if not text:
            raise ValueError("founder_note: content required")

        # Duplicate check — exact-match dupes return the existing
        # row rather than failing. Two cron runs writing the same
        # observation shouldn't blow up the agent's loop.
        existing = self.list(
            business_id=business_id, founder_id=founder_id, store=store,
        )
        for n in existing:
            if n.content.strip() == text:
                return n

        current = sum(len(n.content) for n in existing)
        if current + len(text) > spec.char_limit:
            raise NoteCapacityError(
                store=store,
                current_chars=current,
                attempted_chars=len(text),
                limit=spec.char_limit,
            )

        note = FounderNote(
            business_id=business_id,
            founder_id=founder_id,
            store=store,
            content=text,
        )
        self.session.add(note)
        self.session.commit()
        self.session.refresh(note)
        return note

    def replace(
        self, *,
        business_id: UUID,
        founder_id: UUID,
        store: NoteStoreName,
        old_text: str,
        content: str,
    ) -> FounderNote:
        """Find the unique note containing ``old_text`` (substring
        match) and rewrite its content. Hermes-style — saves the
        agent from emitting full entry bodies."""
        if not old_text.strip():
            raise ValueError("founder_note.replace: old_text required")
        if not content.strip():
            raise ValueError("founder_note.replace: content required")

        spec = STORES.get(store)
        if spec is None:
            raise ValueError(f"unknown store {store!r}")

        target = self._find_unique(
            business_id=business_id,
            founder_id=founder_id,
            store=store,
            substring=old_text,
        )

        # Capacity check across the rest plus the new content.
        existing = self.list(
            business_id=business_id, founder_id=founder_id, store=store,
        )
        other_chars = sum(
            len(n.content) for n in existing if n.id != target.id
        )
        if other_chars + len(content.strip()) > spec.char_limit:
            raise NoteCapacityError(
                store=store,
                current_chars=other_chars,
                attempted_chars=len(content.strip()),
                limit=spec.char_limit,
            )

        target.content = content.strip()
        target.updated_at = _now()
        self.session.add(target)
        self.session.commit()
        self.session.refresh(target)
        return target

    def remove(
        self, *,
        business_id: UUID,
        founder_id: UUID,
        store: NoteStoreName,
        old_text: str,
    ) -> FounderNote:
        if not old_text.strip():
            raise ValueError("founder_note.remove: old_text required")
        target = self._find_unique(
            business_id=business_id,
            founder_id=founder_id,
            store=store,
            substring=old_text,
        )
        self.session.delete(target)
        self.session.commit()
        return target

    def _find_unique(
        self, *,
        business_id: UUID,
        founder_id: UUID,
        store: NoteStoreName,
        substring: str,
    ) -> FounderNote:
        rows = self.list(
            business_id=business_id, founder_id=founder_id, store=store,
        )
        sub = substring.strip()
        matches = [n for n in rows if sub in n.content]
        if not matches:
            raise NoteNotFound(
                f"founder_note: no entry in {store!r} contains "
                f"{substring!r}"
            )
        if len(matches) > 1:
            raise NoteNotFound(
                f"founder_note: substring {substring!r} matches "
                f"{len(matches)} entries in {store!r}; provide a "
                "more specific old_text"
            )
        return matches[0]

    # ---- render ----

    def render_block(
        self, *,
        business_id: UUID,
        founder_id: UUID,
    ) -> str:
        """Build the full system-prompt block for both stores.

        Returns an empty string when both stores are empty so the
        cofounder voice + business context aren't padded with
        empty memory headers on a brand-new install.
        """
        chunks: list[str] = []
        for spec in (USER_STORE, MEMORY_STORE):
            rendered = self._render_one(
                business_id=business_id,
                founder_id=founder_id,
                spec=spec,
            )
            if rendered:
                chunks.append(rendered)
        return "\n\n".join(chunks)

    def _render_one(
        self, *,
        business_id: UUID,
        founder_id: UUID,
        spec: NoteStoreSpec,
    ) -> str:
        rows = self.list(
            business_id=business_id, founder_id=founder_id,
            store=spec.name,
        )
        if not rows:
            return ""
        used = sum(len(r.content) for r in rows)
        pct = int((used / spec.char_limit) * 100)
        bar = (
            "═══════════════════════════════════════════════════"
        )
        body = f"\n{_DELIM}\n".join(r.content for r in rows)
        return (
            f"{bar}\n"
            f"{spec.header} [{pct}% — {used}/{spec.char_limit} chars]\n"
            f"{bar}\n"
            f"{body}"
        )


__all__ = [
    "FounderNote",
    "FounderNoteService",
    "MEMORY_STORE",
    "NoteCapacityError",
    "NoteNotFound",
    "NoteStoreName",
    "NoteStoreSpec",
    "STORES",
    "USER_STORE",
]
