"""ApprovalGate tests."""
from __future__ import annotations

from sqlmodel import Session, select

from korpha.approvals.gate import (
    ApprovalGate,
    Decision,
    ProposalAccepted,
    ProposalDenied,
    ProposalPending,
)
from korpha.approvals.model import (
    ActionClass,
    ApprovalStatus,
    AutonomyMode,
)
from korpha.audit.model import Activity
from korpha.business.model import Business
from korpha.cofounder.model import AgentRole
from korpha.identity.model import Founder


def test_propose_creates_pending_in_draft_mode(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        proposal_summary="Tweet about beta",
        action_payload={"text": "We're live!"},
    )
    assert isinstance(result, ProposalPending)


def test_propose_auto_executes_when_envelope_auto(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)
    gate.set_mode(
        business_id=business.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        mode=AutonomyMode.AUTO,
        actor_id=founder.id,
    )

    result = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        proposal_summary="Tweet about beta",
    )
    assert isinstance(result, ProposalAccepted)
    assert result.auto_executed


def test_propose_denied_when_off(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)
    gate.set_mode(
        business_id=business.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        mode=AutonomyMode.OFF,
        actor_id=founder.id,
    )

    result = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        proposal_summary="Tweet about beta",
    )
    assert isinstance(result, ProposalDenied)


def test_approve_increments_envelope_counter(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)

    pending = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.EMAIL_REPLY,
        proposal_summary="Reply to support thread",
    )
    assert isinstance(pending, ProposalPending)

    decision = gate.decide(
        approval_id=pending.approval_id,
        decision=Decision.APPROVE,
        decided_by_founder_id=founder.id,
    )
    assert decision.approval.status == ApprovalStatus.APPROVED
    assert decision.envelope.consecutive_approvals == 1
    assert decision.promotion_offered is False


def test_modification_resets_envelope_counter(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)

    # Build counter up to 2
    for _ in range(2):
        pending = gate.propose(
            business_id=business.id,
            agent_role_id=cmo.id,
            action_class=ActionClass.EMAIL_REPLY,
            proposal_summary="Reply",
        )
        assert isinstance(pending, ProposalPending)
        gate.decide(
            approval_id=pending.approval_id,
            decision=Decision.APPROVE,
            decided_by_founder_id=founder.id,
        )

    # Now approve with edits → counter resets
    pending = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.EMAIL_REPLY,
        proposal_summary="Reply",
    )
    assert isinstance(pending, ProposalPending)
    result = gate.decide(
        approval_id=pending.approval_id,
        decision=Decision.APPROVE_WITH_EDITS,
        decided_by_founder_id=founder.id,
        modification_note="Tone tweak",
    )
    assert result.approval.status == ApprovalStatus.MODIFIED
    assert result.envelope.consecutive_approvals == 0


def test_rejection_resets_envelope_counter(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)

    pending = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.EMAIL_REPLY,
        proposal_summary="Reply",
    )
    assert isinstance(pending, ProposalPending)
    gate.decide(
        approval_id=pending.approval_id,
        decision=Decision.APPROVE,
        decided_by_founder_id=founder.id,
    )

    pending2 = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.EMAIL_REPLY,
        proposal_summary="Reply",
    )
    assert isinstance(pending2, ProposalPending)
    result = gate.decide(
        approval_id=pending2.approval_id,
        decision=Decision.REJECT,
        decided_by_founder_id=founder.id,
    )
    assert result.approval.status == ApprovalStatus.REJECTED
    assert result.envelope.consecutive_approvals == 0


def test_promotion_offered_at_threshold(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    """After 5 consecutive approvals (default), gate offers auto-promotion."""
    gate = ApprovalGate(session)

    for i in range(5):
        pending = gate.propose(
            business_id=business.id,
            agent_role_id=cmo.id,
            action_class=ActionClass.EMAIL_REPLY,
            proposal_summary=f"Reply {i}",
        )
        assert isinstance(pending, ProposalPending)
        result = gate.decide(
            approval_id=pending.approval_id,
            decision=Decision.APPROVE,
            decided_by_founder_id=founder.id,
        )
    # Last decision should signal promotion offer
    assert result.envelope.consecutive_approvals == 5
    assert result.promotion_offered is True


def test_promote_to_auto(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)

    env = gate.promote_to_auto(
        business_id=business.id,
        action_class=ActionClass.EMAIL_REPLY,
        platform=None,
        approved_by_founder_id=founder.id,
    )
    assert env.mode == AutonomyMode.AUTO

    # Future proposals auto-execute
    result = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.EMAIL_REPLY,
        proposal_summary="Reply (should auto-execute)",
    )
    assert isinstance(result, ProposalAccepted)
    assert result.auto_executed


def test_per_platform_isolation(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    """Twitter envelope being AUTO does not affect LinkedIn envelope (still DRAFT)."""
    gate = ApprovalGate(session)
    gate.set_mode(
        business_id=business.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        mode=AutonomyMode.AUTO,
        actor_id=founder.id,
    )

    twitter = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        proposal_summary="Tweet",
    )
    linkedin = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="linkedin",
        proposal_summary="LinkedIn post",
    )

    assert isinstance(twitter, ProposalAccepted)
    assert isinstance(linkedin, ProposalPending)


def test_activity_log_emitted_on_propose_and_decide(
    session: Session, business: Business, founder: Founder, cmo: AgentRole
) -> None:
    gate = ApprovalGate(session)
    pending = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.EMAIL_REPLY,
        proposal_summary="Reply",
    )
    assert isinstance(pending, ProposalPending)
    gate.decide(
        approval_id=pending.approval_id,
        decision=Decision.APPROVE,
        decided_by_founder_id=founder.id,
    )

    activities = session.exec(
        select(Activity).where(Activity.business_id == business.id)
    ).all()
    event_types = {a.event_type for a in activities}
    assert "approval.proposed" in event_types
    assert "approval.approved" in event_types
