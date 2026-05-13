"""DB-backed default ``LongTermMemory`` implementation.

Stores entries in the existing SQLModel DB (SQLite for dev,
Postgres for production — same code). Retrieval is keyword-LIKE
today; replaceable by an embedding+vector plugin when a real
provider lands. Solopreneur-scale data (~thousands of memories)
fits comfortably in this approach.

Why DB-backed rather than a separate file:
  - Reuses the existing connection pool + migration story.
  - Multi-tenant filtering is just a WHERE clause.
  - The dashboard can show memories alongside other business
    data without crossing process boundaries.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import select

from korpha.memory.contract import (
    LongTermMemory, MemoryEntry, MemoryQuery,
)
from korpha.memory.model import LongTermMemoryEntry

logger = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"\b[\w-]+\b", re.UNICODE)


class DbLongTermMemory(LongTermMemory):
    """Default memory backend. Synchronous SQLModel under the hood;
    the ABC's async signatures are kept for forward-compatibility
    with embedding-based plugins that need real I/O.

    Construct per-call with a session: ``DbLongTermMemory(session)``.
    Single-active-provider semantics in the registry mean we mostly
    pass session per-call; the constructor takes one for testing
    convenience."""

    name = "db"

    def __init__(self, session: Any) -> None:
        self._session = session

    # ---- writes ----

    async def add(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        text: str,
        tags: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
        namespace_id: UUID | None = None,
    ) -> MemoryEntry:
        text = (text or "").strip()
        if not text:
            raise ValueError("memory text cannot be empty")
        row = LongTermMemoryEntry(
            id=uuid4(),
            business_id=business_id,
            founder_id=founder_id,
            text=text,
            tags=[str(t) for t in tags],
            extra=dict(metadata or {}),
            namespace_id=namespace_id,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return _to_entry(row)

    async def forget(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        memory_id: str,
    ) -> bool:
        try:
            mid = UUID(str(memory_id))
        except ValueError:
            return False
        row = self._session.get(LongTermMemoryEntry, mid)
        if row is None:
            return False
        # Multi-tenant: refuse if the row isn't owned by the caller
        if (
            row.business_id != business_id
            or row.founder_id != founder_id
        ):
            logger.warning(
                "memory.db: cross-tenant forget attempted "
                "(target business=%s founder=%s, caller business=%s "
                "founder=%s)",
                row.business_id, row.founder_id,
                business_id, founder_id,
            )
            return False
        self._session.delete(row)
        self._session.commit()
        return True

    # ---- reads ----

    async def search(self, query: MemoryQuery) -> list[MemoryEntry]:
        """Keyword-LIKE search. Tokenizes the query and ranks each
        candidate by the count of distinct query tokens it contains
        — crude but predictable, and good enough for solopreneur-
        scale data where Mike has dozens of memories, not tens of
        thousands. Embedding-based ranking is what plugins exist
        for."""
        text = (query.text or "").strip()
        if not text:
            return []

        # Build base WHERE: scope to the right business + founder
        stmt = (
            select(LongTermMemoryEntry)
            .where(LongTermMemoryEntry.business_id == query.business_id)
            .where(LongTermMemoryEntry.founder_id == query.founder_id)
        )

        rows = list(self._session.exec(stmt).all())
        tokens = _tokenize(text)
        if not tokens:
            return []

        # In-memory filter + score. The dataset for one solopreneur
        # is small enough to scan without an index hit; the query
        # already returned only their rows.
        candidates: list[tuple[int, LongTermMemoryEntry]] = []
        for row in rows:
            row_tokens = _tokenize(row.text)
            score = sum(
                1 for t in tokens
                if any(t.lower() in rt.lower() for rt in row_tokens)
            )
            if score == 0:
                continue
            if query.tags and not any(t in query.tags for t in row.tags):
                continue
            candidates.append((score, row))

        # Highest score first; tie-break by recency (newest wins
        # for "still fresh" memories)
        candidates.sort(
            key=lambda pair: (pair[0], pair[1].created_at),
            reverse=True,
        )
        limited = candidates[: max(1, query.limit)]
        out: list[MemoryEntry] = []
        for score, row in limited:
            entry = _to_entry(row)
            out.append(MemoryEntry(
                id=entry.id,
                text=entry.text,
                business_id=entry.business_id,
                founder_id=entry.founder_id,
                tags=entry.tags,
                score=float(score) / len(tokens),
                created_at=entry.created_at,
                metadata=entry.metadata,
                namespace_id=entry.namespace_id,
            ))
        return out

    async def close(self) -> None:
        # Session lifecycle is the caller's job; we don't own it.
        return None


def _to_entry(row: LongTermMemoryEntry) -> MemoryEntry:
    return MemoryEntry(
        id=str(row.id),
        text=row.text,
        business_id=row.business_id,
        founder_id=row.founder_id,
        tags=tuple(row.tags or ()),
        score=None,
        created_at=row.created_at,
        metadata=dict(row.extra or {}),
        namespace_id=row.namespace_id,
    )


def _tokenize(text: str) -> list[str]:
    """Pull word-ish tokens out for keyword matching. Lowercased,
    deduplicated. Drops sub-3-char tokens — matching on 'a' / 'is'
    floods every memory."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group()
        if len(token) < 3:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


__all__ = ["DbLongTermMemory"]
