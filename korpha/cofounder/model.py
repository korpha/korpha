"""Cofounder org: AgentRole, Thread, Message.

The CEO (an AgentRole) is the single point of contact. C-suite agents are
hired on demand. Workers report to C-suite, never to Founder.

Threads are conversations between Founder and an AgentRole, per platform.
Sticky window enforced via `sticky_until` on Thread.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class RoleType(StrEnum):
    CEO = "ceo"
    CTO = "cto"
    CMO = "cmo"
    COO = "coo"
    CHIEF_OF_STAFF = "chief_of_staff"
    """Internal triage agent. Aggregates blockers from all agents, dedupes,
    tries cheap resolutions, surfaces only the items that genuinely need
    Founder attention. Never user-facing — Founder sees CEO's consolidated
    digest, not CoS directly."""

    WORKER = "worker"


class ThreadPlatform(StrEnum):
    WEB = "web"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    EMAIL = "email"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"


class ThreadStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class MessageSenderType(StrEnum):
    FOUNDER = "founder"
    AGENT = "agent"
    SYSTEM = "system"


class AgentRole(SQLModel, table=True):
    __tablename__ = "agent_role"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    # PR3: agent's primary BusinessUnit (shared workers leave this null
    # and pick up assignment context per card). Null = applies to the
    # business's default unit.
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )
    role_type: RoleType = Field(index=True)
    title: str
    specialty: str | None = Field(default=None)  # e.g. "designer", "copywriter"
    description: str | None = Field(
        default=None,
        description=(
            "Paragraph-long persona / voice / what-they're-good-at "
            "blurb. Used by the CEO + workforce router when picking "
            "between similar-specialty workers (e.g. 'copywriter who "
            "writes punchy 60-word tweets' vs 'copywriter who writes "
            "800-word teardown blog posts'). Specialty alone is a "
            "keyword; description gives the router context to route "
            "on voice, format, and domain — much better signal than "
            "the single-word specialty when N similar workers exist."
        ),
    )
    is_active: bool = Field(default=True, index=True)
    hired_at: datetime = timestamp_field()
    fired_at: datetime | None = Field(default=None)
    personality_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column()
    )
    inference_tier_default: str = Field(
        default="pro",
        description="Default tier for this role: workhorse | pro | consultant",
    )


class Thread(SQLModel, table=True):
    __tablename__ = "thread"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    founder_id: UUID = Field(foreign_key="founder.id", index=True)
    agent_role_id: UUID = Field(foreign_key="agent_role.id", index=True)
    platform: ThreadPlatform = Field(index=True)
    platform_thread_id: str | None = Field(default=None, index=True)
    topic: str | None = Field(default=None)
    sticky_until: datetime | None = Field(
        default=None,
        description="When the sticky window expires. Null = no sticky.",
    )
    status: ThreadStatus = Field(default=ThreadStatus.ACTIVE, index=True)
    created_at: datetime = timestamp_field()
    last_message_at: datetime = timestamp_field()


class Message(SQLModel, table=True):
    __tablename__ = "message"

    id: UUID = primary_key_field()
    thread_id: UUID = Field(foreign_key="thread.id", index=True)
    sender_type: MessageSenderType
    sender_role_id: UUID | None = Field(default=None, foreign_key="agent_role.id")
    content: str
    attachments: dict[str, Any] = Field(default_factory=dict, sa_column=json_column())
    created_at: datetime = timestamp_field(index=True)


class MessageSummary(SQLModel, table=True):
    """Compressed representation of older Founder ↔ agent dialogue.

    When a thread's raw history grows past the recent-window threshold, the
    summarizer rolls the oldest half into one ``MessageSummary`` row so the
    CEO can keep loading bounded context indefinitely without re-feeding
    every turn into every prompt.
    """

    __tablename__ = "message_summary"

    id: UUID = primary_key_field()
    thread_id: UUID = Field(foreign_key="thread.id", index=True)
    summary_text: str
    covers_until: datetime = Field(
        index=True,
        description=(
            "Inclusive upper bound: messages with created_at <= this timestamp "
            "are considered covered by this summary."
        ),
    )
    message_count: int = Field(default=0, description="How many raw turns this summary represents.")
    created_at: datetime = timestamp_field()
