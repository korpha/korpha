"""Liveness classifier — detect stuck work in the cofounder's flow.

Mike isn't watching. A Codex run can grind for hours, a kanban
card can sit in REVIEW for a week, a card can bounce REVIEW →
IN_PROGRESS three times because the agent keeps shipping the
same flaw. None of those failures fire a notification by
themselves.

This module classifies the current state of the kanban board
and surfaces structured ``StuckSignal`` rows that the dashboard,
CLI, and digest cron all consume. The classifier is read-only —
it doesn't move cards or notify channels itself; that's the
caller's job. Keeping detection separate from action means we
can flag "stuck" without spamming Mike's inbox the moment a
threshold trips.

Inspired by Paperclip's ``services/productivity-review.ts`` +
``recovery/issue-graph-liveness.ts`` (single-tenant shape, no
multi-claimant race detection).
"""
from korpha.liveness.classifier import (
    StuckKind,
    StuckSignal,
    classify_kanban_signals,
    default_thresholds,
)

__all__ = [
    "StuckKind",
    "StuckSignal",
    "classify_kanban_signals",
    "default_thresholds",
]
