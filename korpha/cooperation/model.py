"""CooperationProposal + CrossUnitQueryLog tables.

A proposal is a structured ask between BusinessUnits: terms,
permissions JSON (cross_tree_query, cross_namespace_recall, royalty
split, promo slot count, etc.), status enum.

CrossUnitQueryLog tracks every ``cooperation.ask_about`` call for
the founder's monthly review — shows who asked whom about what.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import (
    json_column, primary_key_field, timestamp_field,
)


class CooperationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    ESCALATED_CEO = "escalated_ceo"
    ESCALATED_FOUNDER = "escalated_founder"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CooperationProposal(SQLModel, table=True):
    """One cross-unit cooperation ask. Voluntary; the receiving unit
    can refuse. CEO arbitrates on disagreement."""

    __tablename__ = "cooperation_proposal"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    from_unit_id: UUID = Field(
        foreign_key="business_unit.id", index=True,
    )
    to_unit_id: UUID = Field(
        foreign_key="business_unit.id", index=True,
    )

    summary: str = Field(
        description=(
            "Short one-liner for the dashboard. Example: "
            "'POD merch around Highland Rogue series, 20% royalty back'."
        ),
    )
    details: str = Field(default="")
    """Markdown-allowed long-form details if needed."""

    proposed_terms: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )
    """Free-form terms: royalty split, exclusivity window, payment
    schedule, etc. The decide flow reads this to populate the
    artifact + kanban card."""

    # Permissions the proposal requests / grants. Read by ask_about
    # authorization + memory.recall cross-namespace grants.
    # Known keys (v1):
    #   "cross_tree_query": bool         — allow ask_about across tree
    #   "cross_namespace_recall": bool   — allow memory.recall across ns
    #   "promo_slot_count": int          — affiliate slot allocation
    #   "royalty_share_pct": float       — POD/KDP cross-promo splits
    permissions: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )

    status: CooperationStatus = Field(
        default=CooperationStatus.PROPOSED, index=True,
    )
    decision_note: str | None = Field(default=None)
    decided_by_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id",
    )

    created_at: datetime = timestamp_field(index=True)
    decided_at: datetime | None = Field(default=None)
    expires_at: datetime | None = Field(default=None)


class CrossUnitQueryLog(SQLModel, table=True):
    """Audit trail for ``cooperation.ask_about`` calls.

    Every cross-unit query lands a row here. Founder monthly review
    surfaces who asked whom about what — shows whether cross-unit
    cooperation is producing value or wasting time.
    """

    __tablename__ = "cross_unit_query_log"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    from_unit_id: UUID = Field(
        foreign_key="business_unit.id", index=True,
    )
    to_unit_id: UUID = Field(
        foreign_key="business_unit.id", index=True,
    )
    asked_by_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id",
    )
    question_summary: str
    """First 200 chars of the question (truncated)."""
    response_summary: str | None = Field(default=None)
    """First 200 chars of the dispatched response (populated after the
    target agent answers)."""
    asked_at: datetime = timestamp_field(index=True)


__all__ = [
    "CooperationProposal",
    "CooperationStatus",
    "CrossUnitQueryLog",
]
