"""``memory.recall_by_date`` — programmatic chronological recall.

Token-free indexing: takes a natural-language date phrase ("last
Thursday", "May 10", "2 months ago"), parses it via the stdlib
``date_parse`` helper, and runs a structured SQL query over the
Thread + Message tables. Returns a chronological dump of what
happened in that window — no embeddings, no LLM call inside the
skill.

The agent calling this skill can then either:
  - read the structured payload directly to answer the founder
    (no extra tokens needed)
  - feed the chronology back to an LLM for summarisation (uses
    tokens for the summary, but the indexing is still free)

Complements ``memory.recall`` (which is semantic / embedding-based).
Use this when the founder anchors on time ("what did we work on
May 10?", "what did we talk about last week?"); use the semantic
one when they anchor on a topic.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import and_, select

from korpha.audit.model import InferenceTier
from korpha.cofounder.model import Message, Thread, ThreadPlatform
from korpha.memory.date_parse import parse_natural_date
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)



_DEFAULT_MAX_MESSAGES = 50
_HARD_MESSAGE_LIMIT = 500


def _format_chronology(
    messages: list[Message],
    thread_index: dict,
    max_message_chars: int = 240,
) -> str:
    """Human-readable chronological digest. Threads as headings,
    messages as bulleted entries with truncated content + role."""
    if not messages:
        return "(no messages in that window)"
    out: list[str] = []
    by_thread: dict = {}
    for m in messages:
        by_thread.setdefault(m.thread_id, []).append(m)
    for thread_id, msgs in sorted(
        by_thread.items(), key=lambda kv: kv[1][0].created_at,
    ):
        t = thread_index.get(thread_id)
        platform = t.platform.value if t else "?"
        topic = t.topic if (t and t.topic) else "(no topic)"
        out.append(f"\n## {platform} · {topic}  ({len(msgs)} msg)")
        for m in msgs[:30]:  # cap per-thread output
            ts = m.created_at.astimezone(UTC).strftime("%H:%M")
            role = m.sender_type.value.lower() if hasattr(m.sender_type, "value") else str(m.sender_type)
            body = (m.content or "").strip().replace("\n", " ")
            if len(body) > max_message_chars:
                body = body[:max_message_chars - 1] + "…"
            out.append(f"  [{ts}] {role}: {body}")
        if len(msgs) > 30:
            out.append(f"  … ({len(msgs) - 30} more messages in this thread)")
    return "\n".join(out).strip()


class MemoryRecallByDateSkill(Skill):
    """Look up what happened in a given time window — by date, not topic.

    Hermes-style "session recall": the founder can ask
    *what did we do last Thursday?* or *what did we discuss in May?*
    and we answer from indexed Thread + Message rows without burning
    any LLM tokens to parse the date or fetch the content.
    """

    spec = SkillSpec(
        name="memory.recall_by_date",
        description=(
            "Recall conversations + work from a specific date or "
            "date range. Use when the founder anchors on TIME "
            "('what did we do last Thursday?', 'what was happening "
            "in May?', 'recap last week'). For topic-anchored "
            "recall ('what's our niche?') use memory.recall instead."
        ),
        parameters={
            "when": (
                "Natural-language date phrase. Accepts: 'today', "
                "'yesterday', 'last Thursday', 'May 10', '2 weeks "
                "ago', '2026-05-10', 'between May 10 and May 14', "
                "'this week', 'last month', etc."
            ),
            "max_messages": (
                "Cap on how many messages to return in the structured "
                "payload (default 50, hard cap 500). Lower for "
                "summary-only intent, higher when the agent needs to "
                "actually read content."
            ),
            "platform": (
                "Optional filter: 'web', 'cli', 'tui', 'email', "
                "'telegram', etc. Default = all platforms."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        when = str(args.get("when") or "").strip()
        if not when:
            raise SkillError(
                "memory.recall_by_date: 'when' is required "
                "(e.g. 'last Thursday', 'May 10', '2 weeks ago')."
            )

        date_range = parse_natural_date(when)
        if date_range is None:
            raise SkillError(
                f"memory.recall_by_date: couldn't parse {when!r} as "
                "a date. Try: 'today', 'last Thursday', 'May 10', "
                "'2 weeks ago', 'this month', or ISO '2026-05-10'."
            )

        try:
            max_messages = int(args.get("max_messages") or _DEFAULT_MAX_MESSAGES)
        except (TypeError, ValueError):
            max_messages = _DEFAULT_MAX_MESSAGES
        max_messages = max(1, min(max_messages, _HARD_MESSAGE_LIMIT))

        platform_filter = str(args.get("platform") or "").strip().lower()
        platform_enum = None
        if platform_filter:
            try:
                platform_enum = ThreadPlatform(platform_filter)
            except ValueError as exc:
                valid = ", ".join(p.value for p in ThreadPlatform)
                raise SkillError(
                    f"memory.recall_by_date: unknown platform "
                    f"{platform_filter!r}. Valid: {valid}"
                ) from exc

        business_id = ctx.business.id

        # Step 1: find threads for this business with any message in
        # the window. Cheap; uses the indexed last_message_at column.
        thread_stmt = select(Thread).where(
            and_(
                Thread.business_id == business_id,
                Thread.last_message_at >= date_range.start,
                Thread.created_at < date_range.end,
            )
        )
        if platform_enum is not None:
            thread_stmt = thread_stmt.where(Thread.platform == platform_enum)
        threads = list(ctx.session.exec(thread_stmt).all())
        thread_index = {t.id: t for t in threads}

        if not threads:
            return SkillResult(
                skill_name=self.spec.name,
                summary=(
                    f"No conversations recorded between "
                    f"{date_range.start.date()} and {date_range.end.date()} "
                    f"({date_range.label})."
                ),
                payload={
                    "date_range": {
                        "start": date_range.start.isoformat(),
                        "end": date_range.end.isoformat(),
                        "label": date_range.label,
                    },
                    "thread_count": 0,
                    "message_count": 0,
                    "threads": [],
                    "messages": [],
                },
            )

        # Step 2: pull messages from those threads in the window.
        msg_stmt = (
            select(Message)
            .where(Message.thread_id.in_(list(thread_index.keys())))  # type: ignore[attr-defined]
            .where(Message.created_at >= date_range.start)
            .where(Message.created_at < date_range.end)
            .order_by(Message.created_at)  # type: ignore[arg-type]
        )
        all_messages = list(ctx.session.exec(msg_stmt).all())
        capped = all_messages[:max_messages]

        # Summary string: chronological digest + counts. No LLM call.
        digest = _format_chronology(capped, thread_index)
        summary_lines = [
            f"{date_range.label}: "
            f"{len(all_messages)} message(s) across {len(threads)} thread(s).",
        ]
        if len(all_messages) > len(capped):
            summary_lines.append(
                f"(Showing first {len(capped)}; pass max_messages "
                "higher to see more.)"
            )
        summary_lines.append("")
        summary_lines.append(digest)
        summary_text = "\n".join(summary_lines)

        return SkillResult(
            skill_name=self.spec.name,
            summary=summary_text,
            payload={
                "date_range": {
                    "start": date_range.start.isoformat(),
                    "end": date_range.end.isoformat(),
                    "label": date_range.label,
                },
                "thread_count": len(threads),
                "message_count": len(all_messages),
                "shown_count": len(capped),
                "threads": [
                    {
                        "id": str(t.id),
                        "platform": t.platform.value,
                        "topic": t.topic,
                        "created_at": t.created_at.isoformat(),
                        "last_message_at": t.last_message_at.isoformat(),
                    }
                    for t in threads
                ],
                "messages": [
                    {
                        "thread_id": str(m.thread_id),
                        "role": m.sender_type.value if hasattr(m.sender_type, "value") else str(m.sender_type),
                        "content": m.content,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in capped
                ],
            },
        )


register(MemoryRecallByDateSkill())


__all__ = ["MemoryRecallByDateSkill"]
