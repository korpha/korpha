"""Cross-run continuation summary.

When a long task hands off across multiple runs (Ralph loop
iterations, kanban-card claims that span days, multi-turn
director attempts), the worker on the next run needs to know
*what already happened* without re-reading the raw transcript.

This module produces a bounded digest: prior summary + extracted
path mentions + blocker recap, capped at a configurable char
limit so the system prompt stays cheap.

Inspired by Paperclip's ``services/issue-continuation-summary.ts``
(simplified — single-tenant, no cross-issue references; that's
its own item).
"""
from korpha.continuation.summary import (
    ContinuationSummary,
    summarize_attempts,
    summarize_goal_history,
)

__all__ = [
    "ContinuationSummary",
    "summarize_attempts",
    "summarize_goal_history",
]
