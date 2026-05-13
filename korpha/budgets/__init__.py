"""Scoped budget policies with hard stops.

Mike's #1 anxiety running an autonomous AI cofounder is the
credit-card bill. A runaway loop or a buggy skill hammering the
Pro tier overnight can torch hundreds of dollars before he wakes
up. This subsystem solves that.

A ``BudgetPolicy`` is a per-business cap on USD spend within a
rolling window. Scopes layer:

  * business — the umbrella ($X / day across everything)
  * agent_role — per-director / per-worker ($Y / day for CMO)
  * tier — per-inference-tier ($Z / day on Pro)

When a policy trips, ``CostTracker.complete()`` raises
``BudgetExceededError`` BEFORE making the next LLM call. The
policy gets marked ``paused`` with a reason; subsequent calls
also fail until Mike resumes it. Resuming resets the window
start so the next call gets a fresh window's budget.

Read-only sweeps (``check`` / ``status``) are cheap — single
SUM(cost_usd) per scope; we run them on every ``complete()``
call. The active policies set is small (typically 1-3) so the
overhead is negligible.

Inspired by Paperclip's ``services/budgets.ts`` (single-tenant
shape, no global "instance" scope, no shared budget pools across
companies).
"""
from korpha.budgets.model import (
    BudgetPolicy,
    BudgetScope,
    BudgetWindow,
)
from korpha.budgets.service import (
    BudgetExceededError,
    BudgetService,
    BudgetStatus,
)

__all__ = [
    "BudgetExceededError",
    "BudgetPolicy",
    "BudgetScope",
    "BudgetService",
    "BudgetStatus",
    "BudgetWindow",
]
