"""Throughput control — windowed caps on total agent actions.

Where :class:`BudgetPolicy` caps USD spend, :class:`ActionThrottle`
caps the **count of actions** (LLM calls + kanban transitions +
business events) within a rolling window. Use when:

  - You're running on a $0-marginal stack (subscription, local) and $
    caps are meaningless but you still want a guardrail on volume.
  - You want a stable usage gauge that doesn't drift as model pricing
    changes underneath you.

Throttles compose: 50 actions/hour AND 500/day AND 5000/week — the
strictest one trips first. The autonomy daemon's :func:`evaluate`
consults active throttles before claiming the next card.
"""
from korpha.throughput.model import ActionThrottle
from korpha.throughput.service import (
    ActionThrottleService,
    ThrottleStatus,
    ThroughputExceededError,
    count_actions_in_window,
)

__all__ = [
    "ActionThrottle",
    "ActionThrottleService",
    "ThrottleStatus",
    "ThroughputExceededError",
    "count_actions_in_window",
]
