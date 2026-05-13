"""Tests for reviewer routing on approvals."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.approvals.gate import ApprovalGate
from korpha.approvals.model import (
    ActionClass, Approval, ApprovalStatus,
)
from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType


def _hire_worker(
    session: Session, business: Business, *,
    specialty: str = "copywriter",
) -> AgentRole:
    return HiringService(session).hire(
        business.id, RoleType.WORKER,
        title=specialty.title(),
        specialty=specialty,
    )


def _hire_cmo(session: Session, business: Business) -> AgentRole:
    return HiringService(session).hire(
        business.id, RoleType.CMO, title="CMO",
    )


# ---- worker approvals route to parent director ----


def test_worker_approval_sets_required_reviewer(
    session: Session, business: Business,
) -> None:
    """A copywriter (parent CMO) staging an approval gets
    required_reviewer_role='cmo' set."""
    worker = _hire_worker(session, business, specialty="copywriter")
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="please review my LinkedIn post",
    )
    approval = session.get(Approval, result.approval_id)
    assert approval.required_reviewer_role == "cmo"


def test_designer_routes_to_cmo(
    session: Session, business: Business,
) -> None:
    worker = _hire_worker(session, business, specialty="designer")
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="hero mockup",
    )
    approval = session.get(Approval, result.approval_id)
    assert approval.required_reviewer_role == "cmo"


def test_support_routes_to_coo(
    session: Session, business: Business,
) -> None:
    worker = _hire_worker(session, business, specialty="support")
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="reply to ticket",
    )
    approval = session.get(Approval, result.approval_id)
    assert approval.required_reviewer_role == "coo"


def test_unknown_specialty_falls_back_to_cto(
    session: Session, business: Business,
) -> None:
    """A worker with no registered personality (custom specialty)
    falls back to CTO as a sensible default."""
    worker = _hire_worker(
        session, business, specialty="data-engineer",
    )
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="run pipeline",
    )
    approval = session.get(Approval, result.approval_id)
    assert approval.required_reviewer_role == "cto"


# ---- C-suite approvals skip the gate ----


def test_csuite_approval_no_required_reviewer(
    session: Session, business: Business,
) -> None:
    """CMO staging an approval goes straight to founder; no
    intermediate gate."""
    cmo = _hire_cmo(session, business)
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="campaign brief",
    )
    approval = session.get(Approval, result.approval_id)
    assert approval.required_reviewer_role is None


# ---- reviewer_decide ----


def test_reviewer_approve_clears_gate(
    session: Session, business: Business,
) -> None:
    worker = _hire_worker(session, business)
    _hire_cmo(session, business)
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="x",
    )

    approved = gate.reviewer_decide(
        approval_id=result.approval_id,
        decision="approve",
        reviewer_role="cmo",
        note="lgtm",
    )
    assert approved.required_reviewer_role is None
    assert approved.status == ApprovalStatus.PENDING  # founder still decides
    assert approved.reviewer_decision == "approve"
    assert approved.reviewer_note == "lgtm"


def test_reviewer_reject_marks_rejected(
    session: Session, business: Business,
) -> None:
    worker = _hire_worker(session, business)
    _hire_cmo(session, business)
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="x",
    )

    rejected = gate.reviewer_decide(
        approval_id=result.approval_id,
        decision="reject",
        reviewer_role="cmo",
        note="off-brand",
    )
    assert rejected.status == ApprovalStatus.REJECTED
    assert rejected.decided_by == "reviewer:cmo"


def test_reviewer_decide_wrong_role_rejected(
    session: Session, business: Business,
) -> None:
    """A CTO can't approve a CMO-routed approval."""
    worker = _hire_worker(session, business)
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="x",
    )
    with pytest.raises(ValueError, match="requires reviewer"):
        gate.reviewer_decide(
            approval_id=result.approval_id,
            decision="approve",
            reviewer_role="cto",
        )


def test_reviewer_decide_invalid_decision(
    session: Session, business: Business,
) -> None:
    worker = _hire_worker(session, business)
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="x",
    )
    with pytest.raises(ValueError, match="approve/reject"):
        gate.reviewer_decide(
            approval_id=result.approval_id,
            decision="ponder", reviewer_role="cmo",
        )


def test_reviewer_decide_when_no_gate_rejected(
    session: Session, business: Business,
) -> None:
    """Founder-bound approvals (C-suite-staged) can't be
    reviewer-decided."""
    cmo = _hire_cmo(session, business)
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="x",
    )
    with pytest.raises(ValueError, match="no pending reviewer"):
        gate.reviewer_decide(
            approval_id=result.approval_id,
            decision="approve", reviewer_role="cmo",
        )


# ---- list helpers ----


def test_list_pending_for_founder_excludes_reviewer_gated(
    session: Session, business: Business,
) -> None:
    worker = _hire_worker(session, business)
    cmo = _hire_cmo(session, business)
    gate = ApprovalGate(session=session)
    # Worker proposal — needs CMO review first
    gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="reviewer-gated",
    )
    # CMO proposal — direct to founder
    gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="founder-direct",
    )

    founder_view = gate.list_pending_for_founder(business.id)
    summaries = [a.proposal_summary for a in founder_view]
    assert "founder-direct" in summaries
    assert "reviewer-gated" not in summaries


def test_list_pending_for_founder_includes_after_reviewer_clears(
    session: Session, business: Business,
) -> None:
    worker = _hire_worker(session, business)
    _hire_cmo(session, business)
    gate = ApprovalGate(session=session)
    result = gate.propose(
        business_id=business.id,
        agent_role_id=worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="x",
    )
    # CMO clears it
    gate.reviewer_decide(
        approval_id=result.approval_id,
        decision="approve", reviewer_role="cmo",
    )
    # Now founder sees it
    founder_view = gate.list_pending_for_founder(business.id)
    assert any(a.id == result.approval_id for a in founder_view)


def test_list_pending_for_reviewer_returns_role_specific(
    session: Session, business: Business,
) -> None:
    """A CMO sees CMO-routed proposals but not CTO-routed ones."""
    cmo_worker = _hire_worker(
        session, business, specialty="copywriter",
    )
    cto_worker = _hire_worker(
        session, business, specialty="custom-cto-thing",
    )
    gate = ApprovalGate(session=session)
    gate.propose(
        business_id=business.id,
        agent_role_id=cmo_worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="cmo-routed",
    )
    gate.propose(
        business_id=business.id,
        agent_role_id=cto_worker.id,
        action_class=ActionClass.INTERNAL,
        proposal_summary="cto-routed",
    )

    cmo_queue = gate.list_pending_for_reviewer(business.id, "cmo")
    cto_queue = gate.list_pending_for_reviewer(business.id, "cto")
    assert {a.proposal_summary for a in cmo_queue} == {"cmo-routed"}
    assert {a.proposal_summary for a in cto_queue} == {"cto-routed"}
