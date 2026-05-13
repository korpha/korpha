"""End-to-end integration test — full cofounder turn.

Founder signs up → CEO is hired → CEO 'reasons' via the Inference Pool
(with cost tracking) → CEO proposes a public post → Approval Gate creates
a pending Approval → Founder approves → trust envelope counter ticks.

Wires every major component: identity, business, hiring, inference, cost
tracking, approvals, trust envelope, activity log. Mock provider keeps it
offline; no real LLM calls.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlmodel import Session, select

from korpha.approvals.gate import ApprovalGate, Decision, ProposalPending
from korpha.approvals.model import ActionClass, AutonomyMode
from korpha.audit.model import Activity, Cost, InferenceTier
from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import RoleType
from korpha.identity.model import Founder
from korpha.inference import (
    CompletionRequest,
    InferencePool,
    Message,
    MockProvider,
    ProviderAccount,
    Role,
    TierPricing,
)
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType


def _account() -> ProviderAccount:
    return ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "mock-flash",
            InferenceTier.PRO: "mock-pro",
            InferenceTier.CONSULTANT: "mock-consultant",
        },
        pricing={
            InferenceTier.PRO: TierPricing(
                input_per_1m_usd=Decimal("0.50"),
                output_per_1m_usd=Decimal("1.00"),
            ),
        },
        api_key="sk-test",
    )


@pytest.mark.asyncio
async def test_full_cofounder_turn(session: Session) -> None:
    # 1. Founder signs up.
    founder = Founder(email="mike@example.com", display_name="Mike")
    session.add(founder)
    session.commit()
    session.refresh(founder)

    # 2. Founder creates a business.
    business = Business(
        founder_id=founder.id,
        name="WidgetCo",
        description="B2B SaaS for solo developers",
    )
    session.add(business)
    session.commit()
    session.refresh(business)

    # 3. Korpha auto-hires CEO.
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    assert ceo.role_type == RoleType.CEO

    # 4. CEO reasons via Inference Pool with cost tracking.
    pool = InferencePool(providers=[MockProvider()], accounts=[_account()])
    tracker = CostTracker(pool=pool)

    request = CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content="You are the CEO of WidgetCo."),
            Message(
                role=Role.USER,
                content="Mike wants $5k MRR in 6 months. Propose this week's plan.",
            ),
        ],
        tier=InferenceTier.PRO,
        session_key=f"ceo-{ceo.id}",
    )
    response = await tracker.complete(
        request,
        session=session,
        business_id=business.id,
        agent_role_id=ceo.id,
    )
    assert response.input_tokens > 0
    assert response.cost_usd > 0

    # Cost row persisted.
    costs = session.exec(
        select(Cost).where(Cost.business_id == business.id)
    ).all()
    assert len(costs) == 1
    assert costs[0].agent_role_id == ceo.id
    assert costs[0].tier == InferenceTier.PRO

    # 5. CEO proposes a public post (Twitter) for Mike's approval.
    gate = ApprovalGate(session)
    pending = gate.propose(
        business_id=business.id,
        agent_role_id=ceo.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        proposal_summary="Tweet announcing WidgetCo beta",
        action_payload={"text": "WidgetCo beta is live for solo devs."},
    )
    assert isinstance(pending, ProposalPending)

    # 6. Mike approves.
    decision = gate.decide(
        approval_id=pending.approval_id,
        decision=Decision.APPROVE,
        decided_by_founder_id=founder.id,
    )

    # Envelope counter ticked, no promotion offer yet (5 needed).
    assert decision.envelope.consecutive_approvals == 1
    assert decision.envelope.mode == AutonomyMode.DRAFT
    assert decision.promotion_offered is False

    # 7. Activity log captured the journey.
    events = {
        a.event_type
        for a in session.exec(
            select(Activity).where(Activity.business_id == business.id)
        ).all()
    }
    assert "agent.hired" in events
    assert "approval.proposed" in events
    assert "approval.approved" in events


@pytest.mark.asyncio
async def test_envelope_promotes_after_five_approvals(session: Session) -> None:
    """After 5 consecutive unmodified approvals, gate offers auto-promotion."""
    founder = Founder(email="alice@example.com", display_name="Alice")
    session.add(founder)
    session.commit()
    session.refresh(founder)

    business = Business(founder_id=founder.id, name="ContentCo")
    session.add(business)
    session.commit()
    session.refresh(business)

    hiring = HiringService(session)
    cmo = hiring.hire(business.id, RoleType.CMO)
    gate = ApprovalGate(session)

    promotion_offered_on: list[int] = []
    for i in range(1, 7):
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
        if result.promotion_offered:
            promotion_offered_on.append(i)

    # Promotion is offered on the 5th approval and persists on subsequent ones
    # since mode is still DRAFT (Founder hasn't accepted the offer yet).
    assert 5 in promotion_offered_on

    # Founder accepts the auto-promotion.
    gate.promote_to_auto(
        business_id=business.id,
        action_class=ActionClass.EMAIL_REPLY,
        platform=None,
        approved_by_founder_id=founder.id,
    )

    # Future proposals auto-execute.
    auto = gate.propose(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.EMAIL_REPLY,
        proposal_summary="Reply 7 (post-promotion)",
    )
    from korpha.approvals.gate import ProposalAccepted

    assert isinstance(auto, ProposalAccepted)
    assert auto.auto_executed
