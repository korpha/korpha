"""LLM-driven summarization for the memory layer.

When a thread's raw history grows past the ``recent_window`` size, the
summarizer compresses everything older than the cutoff into one
``MessageSummary`` row. The MemoryService then loads the most recent summary
+ the recent N raw turns instead of the entire thread, keeping prompt size
bounded indefinitely.

Cost discipline: summarization runs at the **Workhorse** tier (cheapest),
batched once per session, not per turn.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import InferenceTier
from korpha.cofounder.model import (
    Message as DbMessage,
)
from korpha.cofounder.model import (
    MessageSenderType,
    MessageSummary,
)
from korpha.db._base import as_utc
from korpha.inference.pool import InferencePool
from korpha.inference.types import CompletionRequest, Role
from korpha.inference.types import Message as LlmMessage

SUMMARIZER_SYSTEM_PROMPT = (
    "You are the memory archivist for an AI cofounder. You receive a slice "
    "of past conversation between a Founder and their AI cofounder team. "
    "Distill it into a compact, factual summary the cofounder can use as "
    "context in future conversations. Capture: decisions made, constraints "
    "the Founder mentioned, names/tools/numbers, open questions, and the "
    "Founder's stated preferences. Do NOT include filler. Do NOT speculate. "
    "Output 4-10 short bullet points only - no preamble, no closing."
)


@dataclass
class SummarizationResult:
    summary: MessageSummary
    bytes_in: int
    """Raw text size summarized (proxy for cost)."""


@dataclass
class MemorySummarizer:
    """Run summarization passes against persisted threads."""

    session: Session
    pool: InferencePool
    tier: InferenceTier = InferenceTier.WORKHORSE
    max_summary_tokens: int = 600
    """Cap on summary length to keep recent-window prompts bounded."""

    async def summarize_older(
        self,
        *,
        thread_id: UUID,
        cutoff: datetime,
        session_key: str,
    ) -> SummarizationResult | None:
        """Summarize every message in *thread_id* with ``created_at <= cutoff``
        that isn't already covered by an existing summary.

        Returns None if there's nothing new to summarize.
        """
        last_covered = self._latest_covered_until(thread_id)
        stmt = (
            select(DbMessage)
            .where(DbMessage.thread_id == thread_id)
            .where(DbMessage.created_at <= cutoff)
        )
        if last_covered is not None:
            stmt = stmt.where(DbMessage.created_at > last_covered)
        stmt = stmt.order_by(DbMessage.created_at.asc())  # type: ignore[attr-defined]
        rows = list(self.session.exec(stmt).all())
        if not rows:
            return None

        rendered = "\n".join(_render_for_summary(r) for r in rows)
        request = CompletionRequest(
            messages=[
                LlmMessage(role=Role.SYSTEM, content=SUMMARIZER_SYSTEM_PROMPT),
                LlmMessage(role=Role.USER, content=rendered),
            ],
            tier=self.tier,
            session_key=f"summarize:{thread_id}",
            max_tokens=self.max_summary_tokens,
            temperature=0.2,
        )
        # Override session affinity passed in by the caller (caller's prompt
        # cache shouldn't be polluted by summary spans).
        _ = session_key
        response = await self.pool.complete(request)

        summary_text = (response.content or "").strip()
        if not summary_text:
            return None

        last_msg_time = as_utc(rows[-1].created_at)
        if last_msg_time is None:
            return None

        summary = MessageSummary(
            thread_id=thread_id,
            summary_text=summary_text,
            covers_until=last_msg_time,
            message_count=len(rows),
        )
        self.session.add(summary)
        self.session.commit()
        self.session.refresh(summary)
        return SummarizationResult(summary=summary, bytes_in=len(rendered))

    def _latest_covered_until(self, thread_id: UUID) -> datetime | None:
        stmt = (
            select(MessageSummary)
            .where(MessageSummary.thread_id == thread_id)
            .order_by(MessageSummary.covers_until.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = self.session.exec(stmt).first()
        if row is None:
            return None
        return as_utc(row.covers_until)


def _render_for_summary(msg: DbMessage) -> str:
    if msg.sender_type == MessageSenderType.FOUNDER:
        prefix = "Founder"
    elif msg.sender_type == MessageSenderType.AGENT:
        prefix = "Cofounder"
    else:
        prefix = "System"
    return f"{prefix}: {msg.content.strip()}"


__all__ = ["SUMMARIZER_SYSTEM_PROMPT", "MemorySummarizer", "SummarizationResult"]
