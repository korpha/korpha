"""Goal SQLModel — one row per active/historical goal on a thread."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import primary_key_field, timestamp_field


class GoalStatus(StrEnum):
    """Lifecycle states.

    ACTIVE  → judge is evaluating after each turn; loop runs continuations
    PAUSED  → founder explicitly paused (or budget hit, or judge tripped);
              continuations stop, founder must resume
    DONE    → judge said goal is satisfied (or unachievable / blocked)
    CLEARED → founder dropped the goal entirely (kept for audit)
    """

    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    CLEARED = "cleared"


class Goal(SQLModel, table=True):
    """One persistent goal attached to a chat thread.

    Per-thread cardinality: only one goal can be ACTIVE at a time
    on a given thread. The GoalManager enforces this at the API
    level — overlapping goals would race for continuation
    prompts and confuse the founder.
    """

    # 'goal' is already taken by korpha/business/model.py for
    # the higher-level business-objective concept. This one is the
    # Ralph-loop runtime state per chat thread.
    __tablename__ = "agent_goal"

    id: UUID = primary_key_field()
    thread_id: UUID = Field(foreign_key="thread.id", index=True)
    business_id: UUID = Field(foreign_key="business.id", index=True)
    # PR3: scoped to a BusinessUnit. Nullable during backfill; resolver
    # treats null as "applies to the business's default unit".
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )

    text: str = Field(
        description=(
            "The founder's free-form goal statement. "
            "E.g. 'get me 10 paying customers by Friday'."
        ),
    )

    status: GoalStatus = Field(default=GoalStatus.ACTIVE, index=True)
    turns_used: int = Field(default=0)
    max_turns: int = Field(
        default=20,
        description="Cap on judge-driven continuations before auto-pause.",
    )

    last_verdict: str | None = Field(
        default=None,
        description="Latest judge verdict: 'done' / 'continue' / 'skipped'.",
    )
    last_reason: str | None = Field(
        default=None,
        description="Judge's one-line rationale from the most recent eval.",
    )
    paused_reason: str | None = Field(
        default=None,
        description=(
            "Why we auto-paused (turn-budget / parse-failures / "
            "user-paused). None when active or done."
        ),
    )
    consecutive_parse_failures: int = Field(
        default=0,
        description=(
            "Judge-output parse failures in a row. After "
            "DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES, the loop "
            "auto-pauses (catches weak judge models that can't "
            "follow the JSON contract)."
        ),
    )

    created_at: datetime = timestamp_field(index=True)
    updated_at: datetime = timestamp_field()
    finished_at: datetime | None = Field(default=None)
    """When the goal moved to a terminal state (DONE / CLEARED)."""
