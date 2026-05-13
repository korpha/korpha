"""Bounded continuation-summary builder.

Two flavors today:

  * ``summarize_attempts(attempts, ...)`` — given a list of
    ``AttemptResult`` records (whatever the workforce produced
    across prior runs), produce a digest the next run can
    inject as context. Strips noise, extracts file paths the
    LLM mentioned, recaps blocker ids.

  * ``summarize_goal_history(session, goal_id, ...)`` — pull
    the goal's evaluate_after_turn judge verdicts + the kanban
    card history attached to the goal, build the same shape.

Both keep within a char budget. We never exceed it; if data is
larger we truncate the bottom (oldest) and note the truncation
so the LLM understands the gap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID


_DEFAULT_CHAR_BUDGET = 1500
"""Char cap for the continuation block. ~500 tokens. Prefix
caching wins more than dense recap; keep it short."""

_PATH_RE = re.compile(
    r"(?:[/.]?(?:[a-zA-Z0-9_-]+/)+[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]{1,6})|"
    r"(?:https?://[^\s)\]]+)",
)
"""Greedy enough to catch ``korpha/cofounder/ceo.py`` and
``https://example.com/foo`` while skipping bare words."""


@dataclass(frozen=True)
class ContinuationSummary:
    """One bounded digest. ``text`` is what the next run sees;
    everything else is metadata for the dashboard / debugging."""

    text: str
    char_count: int
    """Length of ``text``. Useful for prompt-budget math."""

    paths: tuple[str, ...]
    """File paths / URLs we extracted. Surfaced separately so
    callers can render them as tappable links in dashboards."""

    blocker_ids: tuple[UUID, ...]
    """Blocker ids that were mentioned in the source attempts.
    The next run's caller can decide to surface them or filter."""

    truncated: bool
    """True iff we dropped older entries to stay within budget.
    The text includes a `[older entries truncated]` marker too."""


def _bullet_for_attempt(a) -> str:
    """One-line digest of a single AttemptResult-shaped object.

    Duck-typed: anything with ``role_type``, ``status``,
    ``summary``, ``detail`` works (covers AttemptResult and
    custom containers)."""
    role = getattr(getattr(a, "role_type", None), "value", "?")
    status = getattr(a, "status", "?")
    summary = (getattr(a, "summary", "") or "").strip()
    return f"[{role.upper()}] {status}: {summary[:200]}"


def _extract_paths(text: str, *, limit: int = 8) -> tuple[str, ...]:
    """Pull file paths + URLs out of free text. Deduped, capped
    at ``limit`` so we don't flood the digest."""
    seen: list[str] = []
    for m in _PATH_RE.finditer(text):
        candidate = m.group(0).rstrip(".,;:")
        if candidate not in seen:
            seen.append(candidate)
        if len(seen) >= limit:
            break
    return tuple(seen)


def summarize_attempts(
    attempts: Iterable,
    *,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
    header: str = "Prior runs (most recent last):",
) -> ContinuationSummary:
    """Compress a sequence of attempt records.

    The result is **chronological** (oldest → newest) so the
    LLM sees how the situation evolved. When over budget, we
    drop from the FRONT (oldest) — recent context wins.
    """
    items = list(attempts)
    bullets: list[str] = []
    paths: list[str] = []
    blocker_ids: list[UUID] = []

    for a in items:
        bullets.append(_bullet_for_attempt(a))
        # Path extraction across summary + detail
        for source in (
            getattr(a, "summary", ""),
            getattr(a, "detail", "") or "",
        ):
            for p in _extract_paths(source):
                if p not in paths:
                    paths.append(p)
        for bid in getattr(a, "blocker_ids", []) or []:
            if bid not in blocker_ids:
                blocker_ids.append(bid)

    truncated = False
    body = "\n".join(bullets)
    while len(body) + len(header) + 2 > char_budget and bullets:
        bullets.pop(0)
        truncated = True
        body = "\n".join(bullets)

    if truncated:
        body = "[older entries truncated]\n" + body
    text = f"{header}\n{body}" if body else header
    return ContinuationSummary(
        text=text,
        char_count=len(text),
        paths=tuple(paths),
        blocker_ids=tuple(blocker_ids),
        truncated=truncated,
    )


def summarize_goal_history(
    session,
    goal_id: UUID,
    *,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
) -> Optional[ContinuationSummary]:
    """Build a continuation summary from a Goal's prior evaluate
    cycles. Returns None when the goal has no prior turns yet
    (continuation only matters once we've taken at least one
    turn).

    This implementation is intentionally simple — pull goal
    fields the GoalManager already persists (last_verdict,
    last_reason, turns_used) and synthesize a bullet stream.
    Future work (issue-references) will extend it to pull
    related kanban card history."""
    from korpha.goals.model import Goal

    goal = session.get(Goal, goal_id)
    if goal is None or goal.turns_used == 0:
        return None

    bullets = [
        f"Goal: {goal.text[:200]}",
        f"Turns used: {goal.turns_used}/{goal.max_turns}",
    ]
    if goal.last_verdict:
        bullets.append(
            f"Last judge verdict: {goal.last_verdict}"
            + (
                f" ({goal.last_reason[:140]})"
                if goal.last_reason else ""
            )
        )
    if goal.paused_reason:
        bullets.append(f"Paused: {goal.paused_reason}")
    body = "\n".join(bullets)
    if len(body) > char_budget:
        body = body[: char_budget - 4] + "\n…"
    text = "Continuation context:\n" + body
    return ContinuationSummary(
        text=text,
        char_count=len(text),
        paths=tuple(),
        blocker_ids=tuple(),
        truncated=len(body) > char_budget,
    )


__all__ = [
    "ContinuationSummary",
    "summarize_attempts",
    "summarize_goal_history",
]
