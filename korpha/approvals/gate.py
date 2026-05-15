"""ApprovalGate: enforces autonomy rules and the trust envelope.

When an agent proposes an action, the gate looks at the (business, action_class,
platform) trust envelope and either:

- **AUTO**: execute immediately, log to Activity, return Approved.
- **DRAFT**: create a pending Approval, surface to Founder, return Pending.
- **OFF**: deny — agent must escalate via CEO, return Denied.

When the Founder decides on a pending approval, the gate updates the envelope:
- Approve unmodified  → consecutive_approvals += 1
- Approve with edits  → consecutive_approvals reset to 0 (Founder felt the need to edit)
- Reject              → consecutive_approvals reset to 0
- At threshold        → return offer-to-auto signal (Founder confirms via UI)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
    AutonomyMode,
    TrustEnvelope,
)
from korpha.audit.model import Activity, ActorType
from korpha.config import get_settings
from korpha.db._base import utcnow


class Decision(StrEnum):
    APPROVE = "approve"
    APPROVE_WITH_EDITS = "approve_with_edits"
    REJECT = "reject"


@dataclass(frozen=True)
class ProposalAccepted:
    approval_id: UUID
    auto_executed: bool


@dataclass(frozen=True)
class ProposalPending:
    approval_id: UUID


@dataclass(frozen=True)
class ProposalDenied:
    reason: str


ProposalResult = ProposalAccepted | ProposalPending | ProposalDenied


@dataclass(frozen=True)
class DecisionResult:
    approval: Approval
    envelope: TrustEnvelope
    promotion_offered: bool
    """True when the envelope just hit threshold and Founder should be offered auto-promotion."""


class ApprovalGate:
    """Stateful service backed by a SQLModel session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._settings = get_settings()

    def propose(
        self,
        *,
        business_id: UUID,
        agent_role_id: UUID,
        action_class: ActionClass,
        proposal_summary: str,
        action_payload: dict[str, Any] | None = None,
        platform: str | None = None,
        expires_at: datetime | None = None,
        business_unit_id: UUID | None = None,
    ) -> ProposalResult:
        envelope = self._get_or_create_envelope(business_id, action_class, platform)

        if envelope.mode == AutonomyMode.OFF:
            return ProposalDenied(
                reason=f"Action class {action_class!s} is off for this scope; escalate to CEO.",
            )

        if envelope.mode == AutonomyMode.AUTO:
            approval = self._create_approval(
                business_id=business_id,
                agent_role_id=agent_role_id,
                action_class=action_class,
                platform=platform,
                proposal_summary=proposal_summary,
                action_payload=action_payload,
                status=ApprovalStatus.AUTO_EXECUTED,
                decided_by="auto",
                expires_at=expires_at,
                business_unit_id=business_unit_id,
            )
            self._log(
                business_id=business_id,
                actor_type=ActorType.AGENT,
                actor_id=agent_role_id,
                event_type="approval.auto_executed",
                payload={
                    "approval_id": str(approval.id),
                    "action_class": action_class.value,
                    "platform": platform,
                },
            )
            return ProposalAccepted(approval_id=approval.id, auto_executed=True)

        approval = self._create_approval(
            business_id=business_id,
            agent_role_id=agent_role_id,
            action_class=action_class,
            platform=platform,
            proposal_summary=proposal_summary,
            action_payload=action_payload,
            status=ApprovalStatus.PENDING,
            expires_at=expires_at,
            business_unit_id=business_unit_id,
        )
        self._log(
            business_id=business_id,
            actor_type=ActorType.AGENT,
            actor_id=agent_role_id,
            event_type="approval.proposed",
            payload={
                "approval_id": str(approval.id),
                "action_class": action_class.value,
                "platform": platform,
                "summary": proposal_summary,
            },
        )
        return ProposalPending(approval_id=approval.id)

    def decide(
        self,
        *,
        approval_id: UUID,
        decision: Decision,
        decided_by_founder_id: UUID,
        modification_note: str | None = None,
    ) -> DecisionResult:
        approval = self._require_approval(approval_id)
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"Approval {approval_id} is not pending (status={approval.status.value})"
            )

        envelope = self._get_or_create_envelope(
            approval.business_id, approval.action_class, approval.platform
        )

        now = utcnow()
        approval.decided_at = now
        approval.decided_by = str(decided_by_founder_id)
        # Persist the founder's note on every decision type, not just
        # APPROVE_WITH_EDITS. The dashboard surfaces it as 'Your note:'
        # so the team can see *why* the founder approved / rejected.
        if modification_note is not None:
            approval.modification_note = modification_note

        promotion_offered = False
        if decision == Decision.APPROVE:
            approval.status = ApprovalStatus.APPROVED
            envelope.consecutive_approvals += 1
            if (
                envelope.mode == AutonomyMode.DRAFT
                and envelope.consecutive_approvals >= envelope.threshold
            ):
                promotion_offered = True
            event = "approval.approved"
        elif decision == Decision.APPROVE_WITH_EDITS:
            approval.status = ApprovalStatus.MODIFIED
            envelope.consecutive_approvals = 0
            event = "approval.modified"
        else:  # REJECT
            approval.status = ApprovalStatus.REJECTED
            envelope.consecutive_approvals = 0
            event = "approval.rejected"

        envelope.last_decision_at = now
        envelope.last_updated = now

        self.session.add(approval)
        self.session.add(envelope)
        self.session.commit()
        self.session.refresh(approval)
        self.session.refresh(envelope)

        self._log(
            business_id=approval.business_id,
            actor_type=ActorType.FOUNDER,
            actor_id=decided_by_founder_id,
            event_type=event,
            payload={
                "approval_id": str(approval.id),
                "consecutive_approvals": envelope.consecutive_approvals,
                "promotion_offered": promotion_offered,
            },
        )
        return DecisionResult(
            approval=approval, envelope=envelope, promotion_offered=promotion_offered
        )

    def promote_to_auto(
        self,
        *,
        business_id: UUID,
        action_class: ActionClass,
        platform: str | None,
        approved_by_founder_id: UUID,
    ) -> TrustEnvelope:
        """Founder accepts the offer to flip mode to AUTO."""
        envelope = self._get_or_create_envelope(business_id, action_class, platform)
        envelope.mode = AutonomyMode.AUTO
        envelope.last_updated = utcnow()
        self.session.add(envelope)
        self.session.commit()
        self.session.refresh(envelope)

        self._log(
            business_id=business_id,
            actor_type=ActorType.FOUNDER,
            actor_id=approved_by_founder_id,
            event_type="envelope.promoted_to_auto",
            payload={
                "action_class": action_class.value,
                "platform": platform,
                "consecutive_approvals_at_promotion": envelope.consecutive_approvals,
            },
        )
        return envelope

    def set_mode(
        self,
        *,
        business_id: UUID,
        action_class: ActionClass,
        platform: str | None,
        mode: AutonomyMode,
        actor_id: UUID,
    ) -> TrustEnvelope:
        """Founder explicitly sets the autonomy mode (e.g. revoking auto)."""
        envelope = self._get_or_create_envelope(business_id, action_class, platform)
        previous = envelope.mode
        envelope.mode = mode
        envelope.consecutive_approvals = 0
        envelope.last_updated = utcnow()
        self.session.add(envelope)
        self.session.commit()
        self.session.refresh(envelope)

        self._log(
            business_id=business_id,
            actor_type=ActorType.FOUNDER,
            actor_id=actor_id,
            event_type="envelope.mode_changed",
            payload={
                "action_class": action_class.value,
                "platform": platform,
                "from": previous.value,
                "to": mode.value,
            },
        )
        return envelope

    def envelope(
        self,
        *,
        business_id: UUID,
        action_class: ActionClass,
        platform: str | None,
    ) -> TrustEnvelope:
        return self._get_or_create_envelope(business_id, action_class, platform)

    def _get_or_create_envelope(
        self,
        business_id: UUID,
        action_class: ActionClass,
        platform: str | None,
    ) -> TrustEnvelope:
        stmt = select(TrustEnvelope).where(
            TrustEnvelope.business_id == business_id,
            TrustEnvelope.action_class == action_class,
            TrustEnvelope.platform == platform,
        )
        existing = self.session.exec(stmt).one_or_none()
        if existing is not None:
            return existing
        env = TrustEnvelope(
            business_id=business_id,
            action_class=action_class,
            platform=platform,
            threshold=self._settings.trust_envelope_default,
            mode=AutonomyMode.DRAFT,
        )
        self.session.add(env)
        self.session.commit()
        self.session.refresh(env)
        return env

    def _create_approval(
        self,
        *,
        business_id: UUID,
        agent_role_id: UUID,
        action_class: ActionClass,
        platform: str | None,
        proposal_summary: str,
        action_payload: dict[str, Any] | None,
        status: ApprovalStatus,
        decided_by: str | None = None,
        expires_at: datetime | None = None,
        business_unit_id: UUID | None = None,
    ) -> Approval:
        # Reviewer routing: if the staging agent is a WORKER, set
        # required_reviewer_role to the parent so the founder
        # doesn't see it until the parent director signs off.
        # Auto-executed approvals skip the gate (already decided).
        required_reviewer = None
        if status == ApprovalStatus.PENDING:
            required_reviewer = self._maybe_required_reviewer(
                agent_role_id=agent_role_id,
            )

        approval = Approval(
            business_id=business_id,
            business_unit_id=business_unit_id,
            agent_role_id=agent_role_id,
            action_class=action_class,
            platform=platform,
            proposal_summary=proposal_summary,
            action_payload=action_payload or {},
            status=status,
            decided_by=decided_by,
            decided_at=utcnow() if decided_by is not None else None,
            expires_at=expires_at,
            required_reviewer_role=required_reviewer,
        )
        self.session.add(approval)
        self.session.commit()
        self.session.refresh(approval)
        return approval

    def _maybe_required_reviewer(
        self, *, agent_role_id: UUID,
    ) -> str | None:
        """If the staging agent is a worker with a known parent
        director, return the parent role's lowercase value
        (e.g. 'cmo'). Otherwise None — approvals from C-suite
        agents skip the intermediate review and go straight to
        the founder."""
        from korpha.cofounder.director import (
            DEFAULT_WORKER_PERSONALITIES,
        )
        from korpha.cofounder.model import AgentRole, RoleType

        role = self.session.get(AgentRole, agent_role_id)
        if role is None or role.role_type != RoleType.WORKER:
            return None
        if role.specialty:
            spec = DEFAULT_WORKER_PERSONALITIES.get(role.specialty)
            if spec is not None:
                return spec.parent_role_type.value
        # Worker with no registered personality → fall back to CTO
        # (the workforce's default-fallback director).
        return "cto"

    def reviewer_decide(
        self,
        *,
        approval_id: UUID,
        decision: str,
        reviewer_role: str,
        note: str | None = None,
    ) -> Approval:
        """A C-suite director approves or rejects a worker's
        approval. ``decision='approve'`` clears the reviewer gate
        so the founder sees the approval as a normal pending
        item. ``decision='reject'`` marks the approval REJECTED
        + records the director as decided_by."""
        if decision not in ("approve", "reject"):
            raise ValueError(
                f"reviewer_decide: decision must be approve/reject; "
                f"got {decision!r}",
            )
        approval = self._require_approval(approval_id)
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"approval {approval_id} not pending "
                f"(status={approval.status.value})",
            )
        if approval.required_reviewer_role is None:
            raise ValueError(
                "approval has no pending reviewer; founder may "
                "decide directly",
            )
        if approval.required_reviewer_role != reviewer_role.lower():
            raise ValueError(
                f"approval requires reviewer "
                f"{approval.required_reviewer_role!r}, got "
                f"{reviewer_role!r}",
            )

        approval.reviewer_decision = decision
        approval.reviewer_decided_at = utcnow()
        approval.reviewer_note = note

        if decision == "approve":
            # Clear the gate; founder now sees this in pending
            approval.required_reviewer_role = None
        else:
            # Reject — record the rejection now; founder doesn't
            # see this in their inbox.
            approval.status = ApprovalStatus.REJECTED
            approval.decided_at = utcnow()
            approval.decided_by = f"reviewer:{reviewer_role}"
            approval.modification_note = note

        self.session.add(approval)
        self.session.commit()
        self.session.refresh(approval)

        self._log(
            business_id=approval.business_id,
            actor_type=ActorType.AGENT,
            actor_id=approval.agent_role_id,
            event_type=f"approval.reviewer_{decision}",
            payload={
                "approval_id": str(approval.id),
                "reviewer_role": reviewer_role,
                "note": note,
            },
        )
        return approval

    def list_pending_for_founder(
        self, business_id: UUID,
    ) -> list[Approval]:
        """Approvals the founder should actually see — PENDING
        and *not* awaiting an intermediate reviewer."""
        from sqlmodel import select

        stmt = (
            select(Approval)
            .where(Approval.business_id == business_id)
            .where(Approval.status == ApprovalStatus.PENDING)
            .where(Approval.required_reviewer_role.is_(None))
        )
        return list(self.session.exec(stmt).all())

    def list_pending_for_reviewer(
        self, business_id: UUID, reviewer_role: str,
    ) -> list[Approval]:
        """Approvals waiting on a specific C-suite reviewer.
        Used by the dashboard to show the director their
        review queue."""
        from sqlmodel import select

        stmt = (
            select(Approval)
            .where(Approval.business_id == business_id)
            .where(Approval.status == ApprovalStatus.PENDING)
            .where(
                Approval.required_reviewer_role == reviewer_role.lower(),
            )
        )
        return list(self.session.exec(stmt).all())

    def _require_approval(self, approval_id: UUID) -> Approval:
        approval = self.session.get(Approval, approval_id)
        if approval is None:
            raise KeyError(f"Approval {approval_id} not found")
        return approval

    def _log(
        self,
        *,
        business_id: UUID,
        actor_type: ActorType,
        actor_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> Activity:
        activity = Activity(
            business_id=business_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=event_type,
            payload=payload,
        )
        self.session.add(activity)
        self.session.commit()
        self.session.refresh(activity)
        return activity
