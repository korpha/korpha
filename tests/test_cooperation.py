"""PR8 tests — CooperationProposal + ask_about authorization + audit log
+ STRATEGIC approval class.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlmodel import Session, select

from korpha.approvals.model import ActionClass
from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.cooperation.board import (
    CooperationBoard, CooperationError,
)
from korpha.cooperation.model import (
    CooperationProposal, CooperationStatus, CrossUnitQueryLog,
)
from korpha.identity.model import Founder
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


# ---------------------------------------------------------------------------
# Fixtures: 2 sibling lines + descendant under one
# ---------------------------------------------------------------------------


@pytest.fixture
def tree(
    session: Session, business: Business,
) -> dict[str, BusinessUnit]:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    kdp = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    pod = board.create(
        business_id=business.id, name="POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    romance = board.create(
        business_id=business.id, name="Romance",
        kind=BusinessUnitKind.TYPE, parent_id=kdp.id,
    )
    return {"root": root, "kdp": kdp, "pod": pod, "romance": romance}


def _ctx(session, business, founder) -> SkillContext:
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
    )


# ---------------------------------------------------------------------------
# CooperationProposal model
# ---------------------------------------------------------------------------


def test_strategic_action_class_exists() -> None:
    """PR8 added STRATEGIC for CEO→Founder escalation."""
    assert ActionClass.STRATEGIC == "strategic"


def test_propose_creates_row(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    prop = board.propose(
        business_id=business.id,
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        summary="POD merch around Highland Rogue",
        proposed_terms={"royalty_share_pct": 20.0},
        permissions={"royalty_share_pct": 20.0},
    )
    assert prop.id is not None
    assert prop.status == CooperationStatus.PROPOSED
    assert prop.proposed_terms["royalty_share_pct"] == 20.0


def test_propose_refuses_self_target(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    with pytest.raises(CooperationError, match="must differ"):
        board.propose(
            business_id=business.id,
            from_unit_id=tree["kdp"].id,
            to_unit_id=tree["kdp"].id,
            summary="self-proposal",
        )


def test_propose_refuses_empty_summary(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    with pytest.raises(CooperationError, match="summary required"):
        board.propose(
            business_id=business.id,
            from_unit_id=tree["kdp"].id,
            to_unit_id=tree["pod"].id,
            summary="   ",
        )


def test_propose_refuses_missing_unit(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    with pytest.raises(CooperationError, match="unit not found"):
        board.propose(
            business_id=business.id,
            from_unit_id=tree["kdp"].id,
            to_unit_id=uuid4(),
            summary="x",
        )


# ---------------------------------------------------------------------------
# decide / revoke
# ---------------------------------------------------------------------------


def test_decide_accept_transitions(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    prop = board.propose(
        business_id=business.id,
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        summary="x",
    )
    out = board.decide(
        prop.id, decision=CooperationStatus.ACCEPTED, note="agreed",
    )
    assert out.status == CooperationStatus.ACCEPTED
    assert out.decided_at is not None
    assert out.decision_note == "agreed"


def test_decide_decline_transitions(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    prop = board.propose(
        business_id=business.id,
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        summary="x",
    )
    out = board.decide(
        prop.id, decision=CooperationStatus.DECLINED, note="no fit",
    )
    assert out.status == CooperationStatus.DECLINED


def test_decide_twice_refused(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    prop = board.propose(
        business_id=business.id,
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        summary="x",
    )
    board.decide(prop.id, decision=CooperationStatus.ACCEPTED)
    with pytest.raises(CooperationError, match="already"):
        board.decide(prop.id, decision=CooperationStatus.DECLINED)


def test_revoke_accepted_proposal(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    prop = board.propose(
        business_id=business.id,
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        summary="x",
    )
    board.decide(prop.id, decision=CooperationStatus.ACCEPTED)
    out = board.revoke(prop.id)
    assert out.status == CooperationStatus.REVOKED


# ---------------------------------------------------------------------------
# ask_about authorization
# ---------------------------------------------------------------------------


def test_authorized_sibling_pass(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """KDP and POD share a parent (root) — auth passes."""
    board = CooperationBoard(session)
    assert board.ask_about_authorized(
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
    )


def test_authorized_descendant_to_ancestor(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Romance → KDP (parent) passes."""
    board = CooperationBoard(session)
    assert board.ask_about_authorized(
        from_unit_id=tree["romance"].id,
        to_unit_id=tree["kdp"].id,
    )


def test_authorized_ancestor_to_descendant(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """KDP → Romance (child) passes."""
    board = CooperationBoard(session)
    assert board.ask_about_authorized(
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["romance"].id,
    )


def test_cross_tree_blocked_without_grant(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Romance → POD: cross-tree, no grant → blocked."""
    board = CooperationBoard(session)
    assert not board.ask_about_authorized(
        from_unit_id=tree["romance"].id,
        to_unit_id=tree["pod"].id,
    )


def test_cross_tree_passes_with_accepted_proposal(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Romance → POD with an ACCEPTED proposal granting
    cross_tree_query → passes."""
    board = CooperationBoard(session)
    prop = board.propose(
        business_id=business.id,
        from_unit_id=tree["romance"].id,
        to_unit_id=tree["pod"].id,
        summary="ask about merch fit",
        permissions={"cross_tree_query": True},
    )
    board.decide(prop.id, decision=CooperationStatus.ACCEPTED)
    assert board.ask_about_authorized(
        from_unit_id=tree["romance"].id,
        to_unit_id=tree["pod"].id,
    )


def test_cross_tree_blocked_when_proposal_declined(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """Declined proposal does NOT grant access."""
    board = CooperationBoard(session)
    prop = board.propose(
        business_id=business.id,
        from_unit_id=tree["romance"].id,
        to_unit_id=tree["pod"].id,
        summary="ask",
        permissions={"cross_tree_query": True},
    )
    board.decide(prop.id, decision=CooperationStatus.DECLINED)
    assert not board.ask_about_authorized(
        from_unit_id=tree["romance"].id,
        to_unit_id=tree["pod"].id,
    )


def test_self_query_passes(
    session: Session, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    board = CooperationBoard(session)
    assert board.ask_about_authorized(
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["kdp"].id,
    )


# ---------------------------------------------------------------------------
# Skill — cooperation.ask_about + propose + decide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_about_skill_logs_query(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    skill = default_registry.skills["cooperation.ask_about"]
    out = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "from_unit_id": str(tree["kdp"].id),
            "to_unit_id": str(tree["pod"].id),
            "question": "Got merch capacity for Highland Rogue series?",
        },
    )
    # PR-INT-6 — sync dispatch now answers in-process; status is
    # "answered" and the log row carries a response_summary.
    assert out.payload["status"] == "answered"
    assert "response" in out.payload
    logs = list(session.exec(select(CrossUnitQueryLog)).all())
    assert len(logs) == 1
    assert logs[0].from_unit_id == tree["kdp"].id
    assert logs[0].to_unit_id == tree["pod"].id
    assert "Highland Rogue" in logs[0].question_summary
    assert logs[0].response_summary  # captured by dispatcher


@pytest.mark.asyncio
async def test_ask_about_skill_blocks_cross_tree_unauth(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Romance → POD without grant → skill raises."""
    skill = default_registry.skills["cooperation.ask_about"]
    with pytest.raises(SkillError, match="cross-tree query"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "from_unit_id": str(tree["romance"].id),
                "to_unit_id": str(tree["pod"].id),
                "question": "x",
            },
        )


@pytest.mark.asyncio
async def test_propose_skill_then_decide(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    propose = default_registry.skills["cooperation.propose"]
    decide = default_registry.skills["cooperation.decide"]
    p = await propose.run(
        ctx=_ctx(session, business, founder),
        args={
            "from_unit_id": str(tree["kdp"].id),
            "to_unit_id": str(tree["pod"].id),
            "summary": "merch",
        },
    )
    proposal_id = p.payload["proposal_id"]
    d = await decide.run(
        ctx=_ctx(session, business, founder),
        args={
            "proposal_id": proposal_id,
            "decision": "accepted",
            "note": "agreed",
        },
    )
    assert d.payload["status"] == "accepted"


@pytest.mark.asyncio
async def test_decide_rejects_invalid_decision_string(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    propose = default_registry.skills["cooperation.propose"]
    decide = default_registry.skills["cooperation.decide"]
    p = await propose.run(
        ctx=_ctx(session, business, founder),
        args={
            "from_unit_id": str(tree["kdp"].id),
            "to_unit_id": str(tree["pod"].id),
            "summary": "x",
        },
    )
    with pytest.raises(SkillError, match="decision must be"):
        await decide.run(
            ctx=_ctx(session, business, founder),
            args={
                "proposal_id": p.payload["proposal_id"],
                "decision": "maybe",
            },
        )


@pytest.mark.asyncio
async def test_escalate_skill_marks_ceo_arbitration(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    propose = default_registry.skills["cooperation.propose"]
    escalate = default_registry.skills["cooperation.escalate"]
    p = await propose.run(
        ctx=_ctx(session, business, founder),
        args={
            "from_unit_id": str(tree["kdp"].id),
            "to_unit_id": str(tree["pod"].id),
            "summary": "borderline",
        },
    )
    e = await escalate.run(
        ctx=_ctx(session, business, founder),
        args={
            "proposal_id": p.payload["proposal_id"],
            "note": "needs founder input",
        },
    )
    assert e.payload["status"] == "escalated_ceo"
