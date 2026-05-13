"""Business and work entities: Business, Goal, Project, Task.

A Founder runs one or more Businesses. Each Business has Goals (KPI-bound,
hierarchical), Projects (work toward goals), and Tasks (units of work).
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class BusinessStatus(StrEnum):
    IDEA = "idea"
    VALIDATING = "validating"
    LAUNCHED = "launched"
    RUNNING = "running"
    PAUSED = "paused"
    SHUT_DOWN = "shut_down"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    ABANDONED = "abandoned"


class ProjectStatus(StrEnum):
    PLANNING = "planning"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Business(SQLModel, table=True):
    __tablename__ = "business"

    id: UUID = primary_key_field()
    founder_id: UUID = Field(foreign_key="founder.id", index=True)
    name: str
    description: str | None = Field(default=None)
    status: BusinessStatus = Field(default=BusinessStatus.IDEA)
    founder_brief: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=json_column(),
        description=(
            "Day-0 intake: Founder's stated goal, timeline, time budget, "
            "savings, skills, niches considered, constraints. Populated by "
            "the ``founder.intake_brief`` skill on first run; consumed by "
            "downstream skills (niche, validation, pricing) so they don't "
            "have to re-ask. Empty dict means 'not captured yet'."
        ),
    )
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()
    archived_at: datetime | None = Field(default=None)


class Goal(SQLModel, table=True):
    __tablename__ = "goal"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    parent_goal_id: UUID | None = Field(default=None, foreign_key="goal.id")
    title: str
    description: str | None = Field(default=None)
    target_metric: str | None = Field(default=None)
    target_value: float | None = Field(default=None)
    target_date: datetime | None = Field(default=None)
    status: GoalStatus = Field(default=GoalStatus.ACTIVE)
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


class Project(SQLModel, table=True):
    __tablename__ = "project"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    goal_id: UUID | None = Field(default=None, foreign_key="goal.id")
    title: str
    description: str | None = Field(default=None)
    status: ProjectStatus = Field(default=ProjectStatus.PLANNING)
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


class Task(SQLModel, table=True):
    __tablename__ = "task"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    project_id: UUID | None = Field(default=None, foreign_key="project.id")
    parent_task_id: UUID | None = Field(default=None, foreign_key="task.id")
    assigned_to_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id", index=True
    )
    ref_number: int | None = Field(
        default=None,
        index=True,
        description=(
            "Per-business sequential issue number (Linear-style). Combined "
            "with the business prefix to produce a stable human-readable ref "
            "like 'AIG-42'. Allocated by allocate_task_ref() at insert time; "
            "nullable so legacy rows keep loading."
        ),
    )
    title: str
    description: str | None = Field(default=None)
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    priority: TaskPriority = Field(default=TaskPriority.NORMAL)
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()
    completed_at: datetime | None = Field(default=None)
