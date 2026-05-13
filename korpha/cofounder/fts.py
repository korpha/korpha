"""Full-text search across the message table — dual SQLite/Postgres backend.

Layer 3 of the memory stack: when the Founder asks something whose subject
matter was discussed weeks ago — outside the recent-N window and possibly
already summarized — full-text search pulls the most relevant raw turns
back into the prompt.

Backend selection is automatic via SQLAlchemy dialect:

  - **SQLite** (dev / tests): FTS5 virtual table ``message_fts`` mirrors
    ``message.content``. Triggers on the source table keep it in sync —
    application code does NOT need to remember to write to FTS. Rowid is
    a hash of the message UUID; final ranking joins back to ``message``.
  - **Postgres** (production): a GIN expression index on
    ``to_tsvector('english', content)``. No triggers needed — the index
    re-evaluates on insert/update automatically.  Queries use
    ``plainto_tsquery`` so natural-language Founder asks parse without
    user-typed FTS5 syntax.

For both backends ``FtsHit.rank`` follows the lower-is-better convention
(SQLite FTS5 native; Postgres ``ts_rank`` is negated). Callers sort
ascending and take the top N.

Design notes (clean-room reimplementation, FTS5 patterns inspired by
hermes-agent's session search but adapted to Korpha's SQLModel
schema).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, select

from korpha.cofounder.model import (
    Message,
    MessageSenderType,
    Thread,
    ThreadStatus,
)
from korpha.db._base import as_utc

FTS_TABLE = "message_fts"
PG_FTS_INDEX = "idx_message_content_fts"


@dataclass
class FtsHit:
    message_id: UUID
    thread_id: UUID
    sender_type: MessageSenderType
    content: str
    created_at: datetime
    rank: float


def _dialect(session: Session) -> str:
    return session.get_bind().dialect.name


def _is_sqlite(session: Session) -> bool:
    return _dialect(session) == "sqlite"


def _is_postgres(session: Session) -> bool:
    return _dialect(session) == "postgresql"


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------


def ensure_fts_index(session: Session) -> bool:
    """Create the full-text search index if missing.

    Returns True if FTS is available and the index exists, False on
    unsupported dialects (no-op + ``search_messages`` returns []).
    Idempotent — safe to call on every startup.
    """
    if _is_sqlite(session):
        return _ensure_sqlite_fts(session)
    if _is_postgres(session):
        return _ensure_postgres_fts(session)
    return False


def _ensure_sqlite_fts(session: Session) -> bool:
    conn = session.connection()
    # Probe FTS5 availability: try creating a throwaway temp FTS5 table.
    try:
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)"
        )
        conn.exec_driver_sql("DROP TABLE IF EXISTS _fts5_probe")
    except Exception:
        return False

    conn.exec_driver_sql(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
        "  msg_id UNINDEXED,"
        "  thread_id UNINDEXED,"
        "  content"
        ")"
    )

    # Triggers: keep FTS5 in sync with the message table.
    # NOTE: msg_id and thread_id are stored as TEXT (UUID hex) in SQLite
    # via SQLModel's UUID column, so we reference them directly.
    conn.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS message_fts_insert "
        f"AFTER INSERT ON message BEGIN "
        f"  INSERT INTO {FTS_TABLE}(msg_id, thread_id, content) "
        f"  VALUES (new.id, new.thread_id, COALESCE(new.content, '')); "
        f"END"
    )
    conn.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS message_fts_delete "
        f"AFTER DELETE ON message BEGIN "
        f"  DELETE FROM {FTS_TABLE} WHERE msg_id = old.id; "
        f"END"
    )
    conn.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS message_fts_update "
        f"AFTER UPDATE ON message BEGIN "
        f"  DELETE FROM {FTS_TABLE} WHERE msg_id = old.id; "
        f"  INSERT INTO {FTS_TABLE}(msg_id, thread_id, content) "
        f"  VALUES (new.id, new.thread_id, COALESCE(new.content, '')); "
        f"END"
    )

    # Backfill any existing messages that predate the index.
    conn.exec_driver_sql(
        f"INSERT INTO {FTS_TABLE}(msg_id, thread_id, content) "
        f"SELECT id, thread_id, COALESCE(content, '') FROM message "
        f"WHERE id NOT IN (SELECT msg_id FROM {FTS_TABLE})"
    )
    return True


def _ensure_postgres_fts(session: Session) -> bool:
    """Create the GIN expression index for Postgres full-text search.

    No triggers needed — the GIN expression index re-evaluates the
    ``to_tsvector('english', content)`` expression automatically on
    every insert/update. New messages are searchable immediately.

    The index can take a moment to build on tables with many rows; it
    won't block other queries because we're not using ``CONCURRENTLY``
    on first creation (would require running outside a transaction).
    For an empty / small table that's fine.
    """
    conn = session.connection()
    try:
        conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {PG_FTS_INDEX} "
            f"ON message USING GIN (to_tsvector('english', content))"
        )
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Query sanitization (SQLite-only; Postgres plainto_tsquery is forgiving)
# ---------------------------------------------------------------------------


def sanitize_fts5_query(query: str) -> str:
    """Strip FTS5 syntax characters that would otherwise cause parse errors.

    FTS5 has its own query language where ``"``, ``+``, ``(``, ``)``, ``{``,
    ``}``, ``^``, ``*``, and bare ``AND`` / ``OR`` / ``NOT`` are operators.
    Founder queries are natural language, not FTS5 expressions.

    Strategy: preserve correctly balanced quoted phrases so power users can
    do `"exact phrase"`, strip unmatched specials, drop dangling boolean
    operators, and quote dotted/hyphenated tokens so they survive the
    default tokenizer's split.

    Postgres ``plainto_tsquery`` parses arbitrary text safely so this is
    only used on the SQLite path.
    """
    quoted: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        quoted.append(m.group(0))
        return f"\x00Q{len(quoted) - 1}\x00"

    s = re.sub(r'"[^"]*"', _stash, query)
    s = re.sub(r'[+{}()"^]', " ", s)
    s = re.sub(r"\*+", "*", s)
    s = re.sub(r"(^|\s)\*", r"\1", s)
    s = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", s.strip())
    s = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", s.strip())
    s = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', s)
    for i, q in enumerate(quoted):
        s = s.replace(f"\x00Q{i}\x00", q)
    return s.strip()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_messages(
    session: Session,
    *,
    query: str,
    business_id: UUID,
    founder_id: UUID,
    limit: int = 5,
) -> list[FtsHit]:
    """Run a full-text search and return the top ``limit`` hits scoped to
    this Founder + business. Hits are ordered by rank (lower = better).

    Returns ``[]`` for empty queries, when FTS is unavailable, or when
    the sanitized query is empty (e.g. user typed only punctuation).
    """
    if not query.strip():
        return []
    if _is_sqlite(session):
        return _search_sqlite(
            session,
            query=query,
            business_id=business_id,
            founder_id=founder_id,
            limit=limit,
        )
    if _is_postgres(session):
        return _search_postgres(
            session,
            query=query,
            business_id=business_id,
            founder_id=founder_id,
            limit=limit,
        )
    return []


def _allowed_thread_ids(
    session: Session, *, business_id: UUID, founder_id: UUID
) -> list[UUID]:
    stmt = (
        select(Thread.id)
        .where(Thread.business_id == business_id)
        .where(Thread.founder_id == founder_id)
        .where(Thread.status == ThreadStatus.ACTIVE)
    )
    return list(session.exec(stmt).all())


def _search_sqlite(
    session: Session,
    *,
    query: str,
    business_id: UUID,
    founder_id: UUID,
    limit: int,
) -> list[FtsHit]:
    sanitized = sanitize_fts5_query(query)
    if not sanitized:
        return []

    # Pre-filter: thread IDs the user is allowed to see for this business.
    # SQLModel stores UUIDs as 32-char hex without hyphens, so we compare
    # against the .hex form when building the IN clause for FTS5.
    thread_uuids = _allowed_thread_ids(
        session, business_id=business_id, founder_id=founder_id
    )
    thread_ids = [t.hex for t in thread_uuids]
    if not thread_ids:
        return []

    placeholders = ",".join(f":t{i}" for i in range(len(thread_ids)))
    params: dict[str, str | int] = {f"t{i}": tid for i, tid in enumerate(thread_ids)}
    params["q"] = sanitized
    params["lim"] = int(limit)

    sql = text(
        f"SELECT msg_id, thread_id, rank "
        f"FROM {FTS_TABLE} "
        f"WHERE {FTS_TABLE} MATCH :q "
        f"AND thread_id IN ({placeholders}) "
        f"ORDER BY rank "
        f"LIMIT :lim"
    )
    try:
        rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]
    except Exception:
        # Malformed query that survived sanitization — fail soft.
        return []

    if not rows:
        return []

    msg_ids = [UUID(hex=r[0]) for r in rows]
    rank_by_id = {UUID(hex=r[0]): float(r[2] or 0.0) for r in rows}
    msgs = session.exec(select(Message).where(Message.id.in_(msg_ids))).all()  # type: ignore[attr-defined]

    hits: list[FtsHit] = []
    for m in msgs:
        created = as_utc(m.created_at)
        if created is None:
            continue
        hits.append(
            FtsHit(
                message_id=m.id,
                thread_id=m.thread_id,
                sender_type=m.sender_type,
                content=m.content,
                created_at=created,
                rank=rank_by_id.get(m.id, 0.0),
            )
        )
    hits.sort(key=lambda h: h.rank)
    return hits


def _search_postgres(
    session: Session,
    *,
    query: str,
    business_id: UUID,
    founder_id: UUID,
    limit: int,
) -> list[FtsHit]:
    """Postgres FTS via tsvector + plainto_tsquery.

    ``plainto_tsquery`` accepts arbitrary text — no SQLite-style
    sanitization needed. We negate ``ts_rank`` so the FtsHit.rank
    convention stays lower-is-better across both backends.

    Strategy: raw SQL for the rank + filter (so we can use ``@@`` and
    ``ts_rank`` directly), then re-hydrate Message rows via ORM. The
    ORM-loaded Message has properly-decoded enum values; the raw SQL
    return only carries scalar columns we know are safe.
    """
    thread_uuids = _allowed_thread_ids(
        session, business_id=business_id, founder_id=founder_id
    )
    if not thread_uuids:
        return []

    placeholders = ",".join(f":t{i}" for i in range(len(thread_uuids)))
    params: dict[str, object] = {
        f"t{i}": tid for i, tid in enumerate(thread_uuids)
    }
    params["q"] = query
    params["lim"] = int(limit)

    sql = text(
        f"SELECT m.id, "
        f"  ts_rank(to_tsvector('english', m.content), "
        f"          plainto_tsquery('english', :q)) AS rank "
        f"FROM message m "
        f"WHERE m.thread_id IN ({placeholders}) "
        f"  AND to_tsvector('english', m.content) "
        f"      @@ plainto_tsquery('english', :q) "
        f"ORDER BY rank DESC "
        f"LIMIT :lim"
    )
    try:
        rows = session.execute(sql, params).all()
    except Exception:
        return []

    if not rows:
        return []

    msg_ids: list[UUID] = []
    rank_by_id: dict[UUID, float] = {}
    for row in rows:
        raw_id, raw_rank = row[0], row[1]
        msg_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
        msg_ids.append(msg_id)
        # Negate so lower-is-better matches the SQLite convention.
        rank_by_id[msg_id] = -float(raw_rank or 0.0)

    msgs = session.exec(
        select(Message).where(Message.id.in_(msg_ids))  # type: ignore[attr-defined]
    ).all()

    hits: list[FtsHit] = []
    for m in msgs:
        created = as_utc(m.created_at)
        if created is None:
            continue
        hits.append(
            FtsHit(
                message_id=m.id,
                thread_id=m.thread_id,
                sender_type=m.sender_type,
                content=m.content,
                created_at=created,
                rank=rank_by_id.get(m.id, 0.0),
            )
        )
    hits.sort(key=lambda h: h.rank)
    return hits


__all__ = [
    "FTS_TABLE",
    "PG_FTS_INDEX",
    "FtsHit",
    "ensure_fts_index",
    "sanitize_fts5_query",
    "search_messages",
]
