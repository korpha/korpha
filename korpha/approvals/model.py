"""Approval gates and trust envelope.

Default: maximally autonomous internally, draft-for-approval externally.
Per-platform per-action-class autonomy mode. Trust envelope counts
consecutive Founder approvals; at threshold, Korpha offers to flip to auto.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class ActionClass(StrEnum):
    """Categories of action that may need Founder approval."""

    INTERNAL = "internal"  # always auto
    HIRE_AGENT = "hire_agent"
    CODE_CHANGE = "code_change"
    PUBLIC_POST = "public_post"
    EMAIL_OUTREACH = "email_outreach"
    EMAIL_REPLY = "email_reply"
    SPEND = "spend"
    SUPPORT_REPLY = "support_reply"
    COMMERCE = "commerce"
    """Money-receiving / commerce setup actions: create a payment link,
    issue a refund, update a price. Distinct from SPEND (money going out)."""
    STRATEGIC = "strategic"
    """Cross-line strategic decision escalated by CEO when arbitration
    between Line VPs can't be resolved at the C-suite level. PR8 uses
    this for CooperationProposal escalations the CEO bounces to the
    founder."""


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"
    AUTO_EXECUTED = "auto_executed"
    EXPIRED = "expired"


class AutonomyMode(StrEnum):
    DRAFT = "draft"  # propose, wait for Founder approval
    AUTO = "auto"  # execute immediately, log only
    OFF = "off"  # don't even propose; agent must escalate


class Approval(SQLModel, table=True):
    __tablename__ = "approval"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    # PR3: which BusinessUnit this approval belongs to. Nullable during
    # backfill; future migration enforces non-null once every row points
    # at a unit.
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )
    agent_role_id: UUID = Field(foreign_key="agent_role.id", index=True)
    action_class: ActionClass = Field(index=True)
    platform: str | None = Field(default=None, index=True)  # e.g. "twitter"
    proposal_summary: str
    action_payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column()
    )
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, index=True)
    decided_at: datetime | None = Field(default=None)
    decided_by: str | None = Field(
        default=None,
        description="Founder UUID, 'auto' (envelope), or 'expired'",
    )
    modification_note: str | None = Field(default=None)
    created_at: datetime = timestamp_field(index=True)
    expires_at: datetime | None = Field(default=None)

    # Reviewer routing — when a worker stages an approval, the
    # parent C-suite director reviews it before it surfaces to
    # the founder. Cuts founder-inbox volume as the team grows.
    required_reviewer_role: str | None = Field(
        default=None, index=True,
        description=(
            "Pending intermediate reviewer ('cmo' / 'cto' / 'coo'). "
            "Null when the approval is founder-bound directly. "
            "Set automatically by the gate when a worker stages "
            "the approval; cleared once that director signs off."
        ),
    )
    reviewer_decision: str | None = Field(
        default=None,
        description=(
            "Director's decision: 'approve' (pass to founder) / "
            "'reject' (kill the approval). Null until reviewed."
        ),
    )
    reviewer_decided_at: datetime | None = Field(default=None)
    reviewer_note: str | None = Field(
        default=None,
        description="Director's rationale for the founder.",
    )


class TrustEnvelope(SQLModel, table=True):
    __tablename__ = "trust_envelope"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    action_class: ActionClass = Field(index=True)
    platform: str | None = Field(default=None, index=True)
    consecutive_approvals: int = Field(default=0)
    threshold: int = Field(default=5)
    mode: AutonomyMode = Field(default=AutonomyMode.DRAFT)
    last_decision_at: datetime | None = Field(default=None)
    last_updated: datetime = timestamp_field()
