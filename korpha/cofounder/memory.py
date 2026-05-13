"""MemoryService — persistent conversation recall across sessions.

Loads recent Founder ↔ agent messages from the DB and converts them into
the inference-layer ``Message`` shape so the CEO sees prior context every
time the Founder asks something.

Three layers, smallest first:

1. **Recent window** — last N raw turns from the most active thread on this
   business + platform. Cheap, deterministic.
2. **Summarized older context** — when raw window grows past a threshold,
   summarize the older half via Workhorse-tier LLM and replace it with one
   summary turn. Bounds context size while preserving facts. See
   :mod:`korpha.cofounder.summarizer`.
3. **Semantic recall via FTS5** — full-text search across all past messages
   keyed on the current Founder query. Pulls the relevant 5 turns even if
   they were three weeks ago. See :mod:`korpha.cofounder.fts`.

``load_recent`` returns layer 1 only. ``compose`` returns all three layers
merged: latest summary (if any) → de-duped FTS5 hits older than the recent
window → recent N raw turns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlmodel import Session, select

from korpha.cofounder.fts import search_messages
from korpha.cofounder.model import (
    AgentRole,
    MessageSenderType,
    MessageSummary,
    Thread,
    ThreadPlatform,
    ThreadStatus,
)
from korpha.cofounder.model import (
    Message as DbMessage,
)
from korpha.db._base import as_utc, utcnow
from korpha.inference.types import Message as LlmMessage
from korpha.inference.types import Role


@dataclass
class MemoryService:
    """Stateless apart from the SQLModel session."""

    session: Session
    default_window: int = 20
    """Max raw turns to include. Beyond this, summarization kicks in."""

    summarize_above: int = 60
    """If a thread has more than this many raw turns total, prepend the most
    recent summary (if one exists) and don't dig past the recent window for
    raw turns. Summarization itself is triggered explicitly via the
    summarizer service — this just controls how compose() blends results."""

    fts_top_k: int = 4
    """How many FTS5 hits to splice in when ``compose(query=...)`` is called."""

    def load_recent(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        platform: ThreadPlatform | None = None,
        limit: int | None = None,
        max_age_hours: int | None = None,
    ) -> list[LlmMessage]:
        """Return the last `limit` messages across the Founder's active threads
        on this business, in chronological order.

        ``platform=None`` aggregates across all platforms (treats the cofounder
        as one continuous voice). Pass an explicit platform to scope to e.g.
        only the web thread.

        ``max_age_hours`` filters out ancient turns; default = unbounded.
        """
        cap = limit or self.default_window
        if cap <= 0:
            return []

        cutoff: datetime | None = None
        if max_age_hours is not None and max_age_hours > 0:
            cutoff = utcnow().replace(microsecond=0)
            cutoff = cutoff.replace(hour=cutoff.hour) if cutoff.hour else cutoff
            from datetime import timedelta

            cutoff = utcnow() - timedelta(hours=max_age_hours)

        thread_stmt = (
            select(Thread)
            .where(Thread.business_id == business_id)
            .where(Thread.founder_id == founder_id)
            .where(Thread.status == ThreadStatus.ACTIVE)
        )
        if platform is not None:
            thread_stmt = thread_stmt.where(Thread.platform == platform)
        thread_ids = [t.id for t in self.session.exec(thread_stmt).all()]
        if not thread_ids:
            return []

        msg_stmt = (
            select(DbMessage)
            .where(DbMessage.thread_id.in_(thread_ids))  # type: ignore[attr-defined]
            .order_by(DbMessage.created_at.desc())  # type: ignore[attr-defined]
            .limit(cap * 2)
        )
        rows = list(self.session.exec(msg_stmt).all())

        # Filter by age if requested.
        if cutoff is not None:
            rows = [
                r
                for r in rows
                if (created := as_utc(r.created_at)) is not None and created >= cutoff
            ]

        # Take cap most-recent in time (we over-fetched x2 in case of system
        # noise; trim to cap).
        rows = sorted(
            rows[:cap],
            key=lambda r: as_utc(r.created_at) or utcnow(),
        )

        return [_to_llm_message(self.session, r) for r in rows]

    def compose(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        query: str | None = None,
        platform: ThreadPlatform | None = None,
        limit: int | None = None,
    ) -> list[LlmMessage]:
        """Build a memory context that blends all three layers.

        Order of returned messages (oldest semantic context first, newest raw
        turns last — the way an LLM expects chronological context):

        1. Most recent ``MessageSummary`` (if one exists for any of the
           candidate threads) as a ``system`` message.
        2. ``fts_top_k`` FTS5 hits matching ``query`` *that aren't already in
           the recent window* (keeps the prompt cache happy by not repeating
           recent turns at older positions).
        3. The recent N raw turns (same as ``load_recent``).

        ``query=None`` skips layer 2. Useful for context-only loads.
        """
        recent = self.load_recent(
            business_id=business_id,
            founder_id=founder_id,
            platform=platform,
            limit=limit,
        )

        thread_ids = self._candidate_thread_ids(
            business_id=business_id, founder_id=founder_id, platform=platform
        )

        result: list[LlmMessage] = []

        summary_msg = self._latest_summary(thread_ids)
        if summary_msg is not None:
            result.append(summary_msg)

        if query and self.fts_top_k > 0:
            recent_contents = {m.content for m in recent}
            hits = search_messages(
                self.session,
                query=query,
                business_id=business_id,
                founder_id=founder_id,
                limit=self.fts_top_k,
            )
            for hit in hits:
                if hit.content in recent_contents:
                    continue
                result.append(
                    LlmMessage(
                        role=_sender_to_role(hit.sender_type),
                        content=f"[from {hit.created_at:%Y-%m-%d}] {hit.content}",
                    )
                )

        result.extend(recent)
        return result

    def _candidate_thread_ids(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        platform: ThreadPlatform | None,
    ) -> list[UUID]:
        stmt = (
            select(Thread.id)
            .where(Thread.business_id == business_id)
            .where(Thread.founder_id == founder_id)
            .where(Thread.status == ThreadStatus.ACTIVE)
        )
        if platform is not None:
            stmt = stmt.where(Thread.platform == platform)
        return list(self.session.exec(stmt).all())

    def _latest_summary(self, thread_ids: list[UUID]) -> LlmMessage | None:
        if not thread_ids:
            return None
        stmt = (
            select(MessageSummary)
            .where(MessageSummary.thread_id.in_(thread_ids))  # type: ignore[attr-defined]
            .order_by(MessageSummary.covers_until.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = self.session.exec(stmt).first()
        if row is None:
            return None
        return LlmMessage(
            role=Role.SYSTEM,
            content=f"[memory summary, covers up to {row.covers_until:%Y-%m-%d}]\n{row.summary_text}",
        )


def _to_llm_message(session: Session, row: DbMessage) -> LlmMessage:
    role = _sender_to_role(row.sender_type)
    name = None
    if row.sender_type == MessageSenderType.AGENT and row.sender_role_id is not None:
        agent = session.get(AgentRole, row.sender_role_id)
        if agent is not None:
            name = agent.title or agent.role_type.value
    return LlmMessage(role=role, content=row.content, name=name)


def _sender_to_role(sender: MessageSenderType) -> Role:
    if sender == MessageSenderType.FOUNDER:
        return Role.USER
    if sender == MessageSenderType.AGENT:
        return Role.ASSISTANT
    return Role.SYSTEM
