"""Per-session CRUD + auth + lifecycle for CooperationProposal +
CrossUnitQueryLog. Pattern mirrors ``KanbanBoard`` and
``BusinessUnitBoard``.

The ``ask_about_authorized()`` helper drives the cooperation.ask_about
skill: sibling-pass, ancestor/descendant-pass, cross-tree-needs-grant.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.business_units.board import BusinessUnitBoard
from korpha.cooperation.model import (
    CooperationProposal, CooperationStatus, CrossUnitQueryLog,
)


class CooperationError(Exception):
    """Raised when an operation violates a cooperation invariant."""


class CooperationBoard:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.units = BusinessUnitBoard(session)

    # ---- create ----

    def propose(
        self,
        *,
        business_id: UUID,
        from_unit_id: UUID,
        to_unit_id: UUID,
        summary: str,
        details: str = "",
        proposed_terms: dict[str, Any] | None = None,
        permissions: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> CooperationProposal:
        """Validate + insert. Both units must belong to same business."""
        if not summary.strip():
            raise CooperationError("cooperation: summary required")
        if from_unit_id == to_unit_id:
            raise CooperationError(
                "cooperation: from_unit and to_unit must differ"
            )
        from_u = self.units.get(from_unit_id)
        to_u = self.units.get(to_unit_id)
        if from_u is None or to_u is None:
            raise CooperationError("cooperation: unit not found")
        if from_u.business_id != business_id or to_u.business_id != business_id:
            raise CooperationError(
                "cooperation: cross-business cooperation not allowed"
            )
        prop = CooperationProposal(
            business_id=business_id,
            from_unit_id=from_unit_id,
            to_unit_id=to_unit_id,
            summary=summary.strip(),
            details=details,
            proposed_terms=proposed_terms or {},
            permissions=permissions or {},
            expires_at=expires_at,
        )
        self.session.add(prop)
        self.session.commit()
        self.session.refresh(prop)
        return prop

    # ---- decide ----

    def decide(
        self,
        proposal_id: UUID,
        *,
        decision: CooperationStatus,
        note: str | None = None,
        decided_by_agent_role_id: UUID | None = None,
    ) -> CooperationProposal:
        """Accept / decline / escalate. Hook for derived
        CrossNamespaceRecallGrant rows fires on ACCEPTED — wired in
        PR9 (memory namespacing).
        """
        prop = self.session.get(CooperationProposal, proposal_id)
        if prop is None:
            raise CooperationError(
                f"proposal {proposal_id} not found"
            )
        if prop.status != CooperationStatus.PROPOSED and \
           prop.status != CooperationStatus.ESCALATED_CEO:
            raise CooperationError(
                f"proposal already {prop.status.value}"
            )
        if decision not in {
            CooperationStatus.ACCEPTED,
            CooperationStatus.DECLINED,
            CooperationStatus.ESCALATED_CEO,
            CooperationStatus.ESCALATED_FOUNDER,
        }:
            raise CooperationError(
                f"cooperation.decide: invalid decision {decision.value}"
            )
        prop.status = decision
        prop.decision_note = note
        prop.decided_by_agent_role_id = decided_by_agent_role_id
        prop.decided_at = datetime.now(UTC)
        self.session.add(prop)
        self.session.commit()
        self.session.refresh(prop)

        # Derived-grant hook (PR9). On ACCEPTED, if the proposal grants
        # cross_namespace_recall, auto-issue a CrossNamespaceRecallGrant.
        if (
            decision == CooperationStatus.ACCEPTED
            and (prop.permissions or {}).get("cross_namespace_recall")
        ):
            from korpha.memory.grants import issue_grant_from_proposal
            try:
                issue_grant_from_proposal(
                    self.session,
                    proposal_id=prop.id,
                    from_unit_id=prop.from_unit_id,
                    to_unit_id=prop.to_unit_id,
                    granted_by_agent_role_id=decided_by_agent_role_id,
                    expires_at=prop.expires_at,
                )
            except Exception:  # noqa: BLE001
                # Don't fail the decision; the grant is best-effort.
                import logging
                logging.getLogger(__name__).warning(
                    "cooperation: grant issue failed", exc_info=True,
                )

        return prop

    def revoke(self, proposal_id: UUID) -> CooperationProposal:
        """Flip ACCEPTED → REVOKED. Used when a cooperation is later
        unwound (founder retroactive disapproval, list-burn detected,
        etc.). PR9 hook will flip derived recall grants to is_active=False."""
        prop = self.session.get(CooperationProposal, proposal_id)
        if prop is None:
            raise CooperationError(
                f"proposal {proposal_id} not found"
            )
        prop.status = CooperationStatus.REVOKED
        prop.decided_at = datetime.now(UTC)
        self.session.add(prop)
        self.session.commit()
        self.session.refresh(prop)

        # Revoke derived recall grants
        from korpha.memory.grants import revoke_grants_for_proposal
        try:
            revoke_grants_for_proposal(self.session, prop.id)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "cooperation: revoke grants failed", exc_info=True,
            )
        return prop

    # ---- ask_about authorization ----

    def ask_about_authorized(
        self,
        *,
        from_unit_id: UUID,
        to_unit_id: UUID,
    ) -> bool:
        """Default-pass for sibling / ancestor / descendant axis.
        Cross-tree requires an active ACCEPTED proposal granting
        ``cross_tree_query``."""
        if from_unit_id == to_unit_id:
            return True

        from_ancestors = {u.id for u in self.units.ancestors(from_unit_id)}
        to_ancestors = {u.id for u in self.units.ancestors(to_unit_id)}

        # Descendant → ancestor
        if to_unit_id in from_ancestors:
            return True
        # Ancestor → descendant
        if from_unit_id in to_ancestors:
            return True
        # Sibling (share a parent)
        from_unit = self.units.get(from_unit_id)
        to_unit = self.units.get(to_unit_id)
        if from_unit is None or to_unit is None:
            return False
        if (
            from_unit.parent_id is not None
            and from_unit.parent_id == to_unit.parent_id
        ):
            return True

        # Cross-tree — requires an active ACCEPTED CooperationProposal
        # granting cross_tree_query.
        active = self.session.exec(
            select(CooperationProposal).where(
                CooperationProposal.from_unit_id == from_unit_id,
                CooperationProposal.to_unit_id == to_unit_id,
                CooperationProposal.status == CooperationStatus.ACCEPTED,
            )
        ).all()
        for prop in active:
            if (prop.permissions or {}).get("cross_tree_query"):
                return True
        return False

    # ---- audit log ----

    def log_query(
        self,
        *,
        business_id: UUID,
        from_unit_id: UUID,
        to_unit_id: UUID,
        question_summary: str,
        asked_by_agent_role_id: UUID | None = None,
        response_summary: str | None = None,
    ) -> CrossUnitQueryLog:
        row = CrossUnitQueryLog(
            business_id=business_id,
            from_unit_id=from_unit_id,
            to_unit_id=to_unit_id,
            question_summary=(question_summary or "")[:200],
            response_summary=(response_summary or None) and
            response_summary[:200],
            asked_by_agent_role_id=asked_by_agent_role_id,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row


__all__ = ["CooperationBoard", "CooperationError"]
