"""ActionThrottle — per-window cap on total agent actions.

Separate from BudgetPolicy because the unit is **count of actions**,
not USD. An action is any meaningful event the team performs that
consumes CPU or AI: an LLM call, a kanban transition, a skill
invocation, a cron tick, a card creation. Useful when:

  - You want to keep a hard ceiling on activity volume regardless of
    cost (e.g. a local-Ollama setup where $ is meaningless but each
    action still eats GPU + disk + your patience).
  - You're operating shared infrastructure and need to prevent a
    single tenant's runaway loop from starving the rest.
  - You want a usage gauge that's stable across pricing changes —
    the "100 actions/week" number doesn't drift when DeepSeek
    drops their per-token cost in half.

Reuses the same shape as :class:`BudgetPolicy` (window + pause
state + label) so the autonomy engine can treat both uniformly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.budgets.model import BudgetWindow
from korpha.db._base import primary_key_field, timestamp_field


class ActionThrottle(SQLModel, table=True):
    """One action-count cap. Multiple throttles can apply to the same
    business (e.g. 50 actions/hour AND 1000 actions/week); the strictest
    one trips first. The autonomy daemon checks all active throttles
    before claiming the next card."""

    __tablename__ = "action_throttle"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    window: BudgetWindow = Field(
        default=BudgetWindow.DAY,
        description=(
            "Rolling time window. Reuses BudgetWindow so we don't "
            "fragment the time-bucket vocabulary across modules."
        ),
    )
    limit: int = Field(
        description=(
            "Max number of actions allowed within the window. An "
            "action = 1 row in activity / cost / kanban_card_event. "
            "We count the union of those three tables; each row is "
            "one action."
        ),
    )

    # Operational state — mirrors BudgetPolicy so the same UI pattern
    # (paused pill + Resume button) renders identically.
    is_active: bool = Field(default=True, index=True)
    paused_reason: str | None = Field(default=None)
    paused_at: datetime | None = Field(default=None)

    last_window_start: datetime | None = Field(
        default=None,
        description=(
            "Set on resume() so the next window starts fresh, "
            "preventing immediate re-trip from pre-pause overage."
        ),
    )

    label: str = Field(
        default="",
        description=(
            "Free-form human-readable label. Shown on /app/autonomy "
            "and the throughput CLI."
        ),
    )

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


__all__ = ["ActionThrottle"]
