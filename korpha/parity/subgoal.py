"""``/subgoal`` — append criteria to the active /goal without resetting.

Use case: agent's working through ``goal: "ship the landing page"``
and the founder realizes "...and the email signup form must validate".
``/subgoal "form must validate emails"`` appends that as additional
acceptance criteria. The goal-completion check then requires both
the original goal AND every active subgoal to be hit before declaring
done.

Lives next to the existing GoalManager (korpha.goals); a subgoal
is just a Goal row with parent_goal_id set + status=ACTIVE and a
shorter description.

Format the agent sees in its prompt::

    Active goal: ship the landing page
    Acceptance criteria added since:
      - form must validate emails
      - mobile breakpoint under 640px works
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4


@dataclass(frozen=True)
class SubgoalEntry:
    """One additional criterion appended to the active goal."""

    id: UUID
    parent_goal_id: UUID
    description: str
    created_at: datetime
    resolved_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.resolved_at is None


def append_subgoal(
    *, parent_goal_id: UUID, description: str,
) -> SubgoalEntry:
    """Create a new SubgoalEntry. Caller persists it (typically as
    a Goal row with parent_goal_id set)."""
    description = (description or "").strip()
    if not description:
        raise ValueError("subgoal description must be non-empty")
    return SubgoalEntry(
        id=uuid4(),
        parent_goal_id=parent_goal_id,
        description=description,
        created_at=datetime.now(tz=timezone.utc),
    )


def render_active_subgoals(
    parent_goal_description: str,
    subgoals: list[SubgoalEntry],
) -> str:
    """Render the parent + active subgoals as a prompt fragment.
    Empty subgoal list returns just the parent description."""
    actives = [s for s in subgoals if s.active]
    if not actives:
        return f"Active goal: {parent_goal_description}"
    lines = [f"Active goal: {parent_goal_description}"]
    lines.append("Acceptance criteria added since:")
    for s in actives:
        lines.append(f"  - {s.description}")
    return "\n".join(lines)


__all__ = [
    "SubgoalEntry",
    "append_subgoal",
    "render_active_subgoals",
]
