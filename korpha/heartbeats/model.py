"""Wakeup + Routine tables.

A **Wakeup** is a one-shot fire-once-and-done timer. It carries a ``kind``
(string handler key) and an opaque ``payload`` JSON dict. Status transitions:

    pending → in_flight → done
                       ↘ failed
    pending → cancelled

A **Routine** is a recurring source of Wakeups. The dispatcher inspects each
enabled routine on every tick and enqueues a fresh Wakeup whenever the
schedule says it's due. We start with simple interval scheduling
(``every_seconds``); cron expressions can be added later as another
``RoutineSchedule`` discriminator without changing the dispatch path.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class WakeupStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WakeupKind(StrEnum):
    """Built-in wakeup kinds. Custom kinds can use any string — the registry
    maps strings to handlers, so this enum is for discoverability + type
    completion, not enforcement."""

    CEO_DAILY_DIGEST = "ceo.daily_digest"
    """Generate the Chief-of-Staff blocker digest and surface to CEO."""

    CMO_CONTENT_HEARTBEAT = "cmo.content_heartbeat"
    """Daily check on whether the content cadence is on track."""

    FINANCE_WEEKLY_REVIEW = "finance.weekly_review"
    """Weekly P&L review by the COO."""

    SUPPORT_INBOX_SWEEP = "support.inbox_sweep"
    """Triage the support inbox every N hours."""


class RoutineSchedule(StrEnum):
    EVERY_SECONDS = "every_seconds"
    """``schedule_value`` is the interval in seconds. Simplest, most common."""


class Wakeup(SQLModel, table=True):
    __tablename__ = "wakeup"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    kind: str = Field(index=True, description="Handler key — see WakeupKind for built-ins.")
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=json_column())
    fire_at: datetime = Field(index=True, description="When the wakeup becomes eligible.")
    status: WakeupStatus = Field(default=WakeupStatus.PENDING, index=True)
    dedupe_key: str | None = Field(
        default=None,
        index=True,
        description=(
            "Optional caller-supplied key. Scheduling a wakeup with an existing "
            "(business_id, kind, dedupe_key, status=pending) tuple is a no-op — "
            "lets routines re-enqueue idempotently."
        ),
    )
    routine_id: UUID | None = Field(default=None, foreign_key="routine.id", index=True)
    tier_override: str | None = Field(
        default=None,
        description=(
            "Optional InferenceTier override (workhorse / pro / consultant / vision). "
            "When set, handler-invoked LLM calls use this tier instead of the "
            "skill's default. Inherited from the parent routine (if any) at "
            "schedule time."
        ),
    )
    provider_label: str | None = Field(
        default=None,
        description=(
            "Optional provider-account label override. Pins LLM calls in this "
            "wakeup to a specific account from providers.yaml regardless of "
            "session affinity. Inherited from the parent routine."
        ),
    )
    last_error: str | None = Field(default=None)
    attempts: int = Field(default=0)
    created_at: datetime = timestamp_field(index=True)
    fired_at: datetime | None = Field(default=None)


class Routine(SQLModel, table=True):
    __tablename__ = "routine"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    name: str
    kind: str = Field(description="Wakeup kind to enqueue when this routine fires.")
    schedule_kind: RoutineSchedule = Field(default=RoutineSchedule.EVERY_SECONDS)
    schedule_value: int = Field(description="Meaning depends on schedule_kind.")
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=json_column())
    enabled: bool = Field(default=True, index=True)
    tier_override: str | None = Field(
        default=None,
        description=(
            "Optional tier override applied to LLM calls fired by this routine. "
            "Use cases: 'this weekly review goes to Pro', 'this nightly digest "
            "summarizer runs on Workhorse to save subscription quota'."
        ),
    )
    provider_label: str | None = Field(
        default=None,
        description=(
            "Optional provider-account label. Pins this routine's LLM calls to "
            "a specific account from providers.yaml. Use cases: 'memory "
            "summarizer pinned to cheap-api-account so subscription quota is "
            "preserved for chat'."
        ),
    )
    last_fired_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = timestamp_field()
