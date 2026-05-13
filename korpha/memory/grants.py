"""CrossNamespaceRecallGrant + namespace authorization for memory.recall.

When a CooperationProposal with ``permissions["cross_namespace_recall"]
= True`` is ACCEPTED, an active grant row is created linking the
asking unit's namespace to the target unit's. Memory.recall consults
these grants before authorizing foreign-namespace queries.

Grants are revocable — flipping a CooperationProposal to REVOKED
flips ``is_active=False`` on the derived grant, blocking subsequent
recall calls immediately.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Field, SQLModel, Session, select

from korpha.db._base import (
    primary_key_field, timestamp_field, utcnow,
)


class CrossNamespaceRecallGrant(SQLModel, table=True):
    """Authorization for memory.recall across BusinessUnit namespaces.

    Created by the cooperation hook when a proposal carrying
    ``permissions["cross_namespace_recall"]=True`` is ACCEPTED.
    Revoked (is_active=False) when the proposal moves to REVOKED or
    when the founder explicitly removes it.
    """

    __tablename__ = "cross_namespace_recall_grant"

    id: UUID = primary_key_field()
    from_namespace_id: UUID = Field(index=True)
    """The namespace ALLOWED TO READ. (asker's unit namespace)"""
    to_namespace_id: UUID = Field(index=True)
    """The namespace BEING READ. (target's unit namespace)"""

    cooperation_proposal_id: UUID = Field(
        foreign_key="cooperation_proposal.id", index=True,
    )
    """Audit trail back to the proposal that issued this grant."""

    granted_at: datetime = timestamp_field()
    expires_at: datetime | None = Field(default=None)
    granted_by_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id",
    )

    is_active: bool = Field(default=True, index=True)


class CrossNamespaceRecallRefused(Exception):
    """Raised when memory.recall is asked to read a foreign namespace
    without an active CrossNamespaceRecallGrant. Caller surfaces this
    as a SkillError; agent must either fall back to own-namespace
    recall or propose a CooperationProposal."""


def check_recall_authorized(
    session: Session,
    *,
    from_namespace_id: UUID,
    to_namespace_id: UUID,
    now: datetime | None = None,
) -> bool:
    """True if from_ns is permitted to read to_ns. Same-namespace
    queries always pass; cross-namespace requires an active uncexpired
    grant."""
    if from_namespace_id == to_namespace_id:
        return True
    now = now or utcnow()
    rows = session.exec(
        select(CrossNamespaceRecallGrant).where(
            CrossNamespaceRecallGrant.from_namespace_id == from_namespace_id,
            CrossNamespaceRecallGrant.to_namespace_id == to_namespace_id,
            CrossNamespaceRecallGrant.is_active == True,  # noqa: E712
        )
    ).all()
    for grant in rows:
        if grant.expires_at is None:
            return True
        exp = grant.expires_at
        if exp.tzinfo is None:
            from datetime import UTC
            exp = exp.replace(tzinfo=UTC)
        if exp > now:
            return True
    return False


def issue_grant_from_proposal(
    session: Session,
    *,
    proposal_id: UUID,
    from_unit_id: UUID,
    to_unit_id: UUID,
    granted_by_agent_role_id: UUID | None = None,
    expires_at: datetime | None = None,
) -> CrossNamespaceRecallGrant:
    """Hook called when a CooperationProposal with
    ``permissions["cross_namespace_recall"]`` is ACCEPTED."""
    from korpha.business_units.model import BusinessUnit
    from_unit = session.get(BusinessUnit, from_unit_id)
    to_unit = session.get(BusinessUnit, to_unit_id)
    if from_unit is None or to_unit is None:
        raise ValueError(
            "issue_grant_from_proposal: unit not found"
        )
    grant = CrossNamespaceRecallGrant(
        from_namespace_id=from_unit.memory_namespace_id,
        to_namespace_id=to_unit.memory_namespace_id,
        cooperation_proposal_id=proposal_id,
        expires_at=expires_at,
        granted_by_agent_role_id=granted_by_agent_role_id,
        is_active=True,
    )
    session.add(grant)
    session.commit()
    session.refresh(grant)
    return grant


def revoke_grants_for_proposal(
    session: Session, proposal_id: UUID,
) -> int:
    """When a proposal is REVOKED, flip all derived grants is_active=False.
    Returns count of grants revoked."""
    rows = session.exec(
        select(CrossNamespaceRecallGrant).where(
            CrossNamespaceRecallGrant.cooperation_proposal_id == proposal_id,
            CrossNamespaceRecallGrant.is_active == True,  # noqa: E712
        )
    ).all()
    for g in rows:
        g.is_active = False
        session.add(g)
    session.commit()
    return len(rows)


__all__ = [
    "CrossNamespaceRecallGrant",
    "CrossNamespaceRecallRefused",
    "check_recall_authorized",
    "issue_grant_from_proposal",
    "revoke_grants_for_proposal",
]
