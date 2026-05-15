"""Blocker entity + status / kind / urgency enums."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class BlockerKind(StrEnum):
    DECISION = "decision"
    """Need a yes/no or pick-one from Founder."""

    INFO = "info"
    """Need a piece of information the agent cannot derive."""

    APPROVAL = "approval"
    """Need explicit approval — links to or creates an Approval."""

    PERMISSION = "permission"
    """Scope or auth issue (account access, integration not connected)."""

    RESOURCE = "resource"
    """Money, time, or tool not available."""

    CLARIFICATION = "clarification"
    """Ambiguous instruction that needs to be re-stated."""

    OTHER = "other"


class BlockerUrgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class BlockerStatus(StrEnum):
    OPEN = "open"
    """Just submitted — CoS hasn't looked yet."""

    TRIAGED = "triaged"
    """CoS has reviewed and assigned a recommendation but hasn't escalated."""

    RESOLVED_BY_COS = "resolved_by_cos"
    """CoS resolved without involving Founder (cheap resolution)."""

    AWAITING_FOUNDER = "awaiting_founder"
    """Surfaced to CEO; on the next digest to Founder."""

    RESOLVED = "resolved"
    """Founder (or trust envelope) resolved it."""

    DROPPED = "dropped"
    """CoS decided no Founder action is needed (no-op or stale)."""


class Blocker(SQLModel, table=True):
    __tablename__ = "blocker"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    requesting_agent_role_id: UUID = Field(foreign_key="agent_role.id", index=True)
    task_id: UUID | None = Field(default=None, foreign_key="task.id", index=True)
    kanban_card_id: UUID | None = Field(
        default=None, foreign_key="kanban_card.id", index=True,
    )
    """The kanban card this blocker is attached to. Lets /app/kanban/{id}
    surface 'what's blocking this card' and lets the founder respond from
    the card detail page."""

    kind: BlockerKind = Field(index=True)
    urgency: BlockerUrgency = Field(default=BlockerUrgency.NORMAL, index=True)
    status: BlockerStatus = Field(default=BlockerStatus.OPEN, index=True)

    title: str = Field(index=True)
    detail: str = Field(default="")
    options: list[str] = Field(default_factory=list, sa_column=json_column())
    """Choices the agent surfaced. CoS may add more during triage."""

    cos_recommendation: str | None = Field(default=None)
    cos_notes: str | None = Field(default=None)
    topic_tag: str | None = Field(default=None, index=True)
    """CoS-assigned grouping tag — multiple related blockers share a tag."""

    deduped_into_id: UUID | None = Field(default=None, foreign_key="blocker.id")
    """If this is a duplicate, points at the canonical Blocker."""

    parent_blocker_id: UUID | None = Field(default=None, foreign_key="blocker.id")
    """When one blocker is *blocked-on* another (chain)."""

    approval_id: UUID | None = Field(default=None, foreign_key="approval.id")
    """If CoS converted this into an Approval, link it."""

    resolution: str | None = Field(default=None)
    resolution_meta: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column()
    )
    resolved_by_founder_id: UUID | None = Field(default=None, foreign_key="founder.id")

    submitted_at: datetime = timestamp_field(index=True)
    triaged_at: datetime | None = Field(default=None)
    surfaced_at: datetime | None = Field(default=None)
    resolved_at: datetime | None = Field(default=None)
