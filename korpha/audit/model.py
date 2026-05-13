"""Audit and cost tracking.

Activity is an immutable event log. Every mutating action emits one.
Cost tracks token spend per agent / model / task / thread for budget
enforcement and Founder-visible reporting.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class ActorType(StrEnum):
    FOUNDER = "founder"
    AGENT = "agent"
    SYSTEM = "system"


class InferenceTier(StrEnum):
    WORKHORSE = "workhorse"
    PRO = "pro"
    CONSULTANT = "consultant"
    VISION = "vision"
    """Vision = analyzing images (NOT generating them — that's the image
    provider system, separate). Browser screenshots, design review,
    landing-page QA route through this tier. The wizard auto-sets
    vision=pro if the Pro model supports vision (Kimi K2.6, Qwen3-VL,
    Llama-3.2-Vision, GLM-4V, NVIDIA Nemotron 3 Nano Omni, …); otherwise
    it suggests Nemotron 3 Nano Omni via OpenRouter free tier or local."""


class Activity(SQLModel, table=True):
    __tablename__ = "activity"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    # PR3: per-unit activity feed for the dashboard /app/units view.
    # Nullable during backfill; once non-null, monthly review can roll
    # up activity by BusinessUnit.
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )
    actor_type: ActorType = Field(index=True)
    actor_id: UUID | None = Field(default=None, index=True)
    event_type: str = Field(index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=json_column())
    created_at: datetime = timestamp_field(index=True)


class Cost(SQLModel, table=True):
    __tablename__ = "cost"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    # PR3: per-unit P&L attribution. Set from the assignment context,
    # not the agent (shared workers serve multiple units; the call's
    # unit context drives attribution).
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )
    agent_role_id: UUID | None = Field(default=None, foreign_key="agent_role.id")
    task_id: UUID | None = Field(default=None, foreign_key="task.id")
    thread_id: UUID | None = Field(default=None, foreign_key="thread.id")
    provider: str = Field(index=True)
    model: str = Field(index=True)
    tier: InferenceTier = Field(index=True)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cached_tokens: int = Field(default=0)
    cost_usd: Decimal = Field(default=Decimal("0"), max_digits=12, decimal_places=6)
    created_at: datetime = timestamp_field(index=True)
