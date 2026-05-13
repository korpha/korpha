#!/usr/bin/env python3
"""End-to-end demo: real cofounder turn against Ollama Cloud DeepSeek V4 Pro.

Run with:

    OLLAMA_CLOUD_API_KEY=... python scripts/demo.py

or simply ``python scripts/demo.py`` if .env is loaded.

What it does:

1. Creates an in-memory SQLite DB with all tables.
2. Creates a Founder + Business.
3. Hires the CEO.
4. CEO uses real DeepSeek V4 Pro (via Ollama Cloud) to produce a structured Plan.
5. ApprovalGate creates a pending Approval.
6. Founder "approves" — trust envelope counter ticks.
7. Prints the full conversation including reasoning, plan, approval state, and
   the cost charged (Ollama Cloud subscription = $0).

Designed to demonstrate the full system end-to-end, on real models, in
under 60 seconds.
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

# Make `korpha` importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

import korpha.db.registry  # noqa: F401, E402  registers all models
from korpha.approvals.gate import (  # noqa: E402
    ApprovalGate,
    Decision,
    ProposalAccepted,
    ProposalDenied,
    ProposalPending,
)
from korpha.audit.model import Activity, Cost, InferenceTier  # noqa: E402
from korpha.business.model import Business  # noqa: E402
from korpha.cofounder.ceo import CEO  # noqa: E402
from korpha.cofounder.hiring import HiringService  # noqa: E402
from korpha.identity.model import Founder  # noqa: E402
from korpha.inference import (  # noqa: E402
    InferencePool,
    ProviderAccount,
    ollama_cloud_provider,
)
from korpha.inference.cost_tracker import CostTracker  # noqa: E402
from korpha.inference.registry import AuthType  # noqa: E402

load_dotenv()


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _build_account(api_key: str) -> ProviderAccount:
    return ProviderAccount(
        provider_name="ollama-cloud",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "deepseek-v4-flash:cloud",
            InferenceTier.PRO: "deepseek-v4-pro:cloud",
        },
        api_key=api_key,
        label="ollama-cloud",
    )


async def main() -> None:
    api_key = os.getenv("OLLAMA_CLOUD_API_KEY")
    if not api_key:
        print("error: OLLAMA_CLOUD_API_KEY not set in environment or .env")
        sys.exit(1)

    print(_bold("=" * 70))
    print(_bold("  Korpha — end-to-end cofounder turn (real DeepSeek V4 Pro)"))
    print(_bold("=" * 70))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        # 1. Sign up
        founder = Founder(email="mike@example.com", display_name="Mike")
        session.add(founder)
        session.commit()
        session.refresh(founder)

        # 2. Create business
        business = Business(
            founder_id=founder.id,
            name="WidgetCo",
            description=(
                "B2B SaaS niche for solo Python developers. "
                "Mike has 5h/week and $2k of savings. Wants $5k MRR in 6 months."
            ),
        )
        session.add(business)
        session.commit()
        session.refresh(business)
        print(f"\n{_bold('Founder:')} {founder.display_name} ({founder.email})")
        print(f"{_bold('Business:')} {business.name} — {business.description}")

        # 3. Hire CEO
        hiring = HiringService(session)
        ceo_role = hiring.ensure_ceo(business.id)
        print(f"{_bold('CEO hired:')} {ceo_role.title} (id={ceo_role.id})")

        # 4. Build inference pool with real Ollama Cloud
        pool = InferencePool(
            providers=[ollama_cloud_provider()],
            accounts=[_build_account(api_key)],
        )
        tracker = CostTracker(pool=pool)
        gate = ApprovalGate(session)
        ceo = CEO(session=session, cost_tracker=tracker, hiring=hiring, gate=gate)

        # 5. CEO proposes a plan via real DeepSeek V4 Pro
        prompt = (
            "Mike wants $5k MRR in 6 months for a B2B SaaS micro-niche aimed at "
            "solo Python developers. He has 5h/week and $2k savings. Propose this "
            "week's plan."
        )
        print(f"\n{_bold('Founder asks:')} {prompt}")
        print(_dim("\n→ CEO is thinking via deepseek-v4-pro:cloud..."))

        plan, proposal = await ceo.propose(
            business=business,
            founder=founder,
            founder_input=prompt,
        )

        # 6. Show what came back
        print(f"\n{_bold(_yellow('Plan'))}")
        print(f"  summary:        {plan.summary}")
        print("  rationale:")
        for r in plan.rationale:
            print(f"    - {r}")
        print(f"  next_action:    {plan.next_action}")
        if plan.estimated_hours is not None:
            print(f"  estimated_hours: {plan.estimated_hours}")
        if plan.expected_impact:
            print(f"  expected_impact: {plan.expected_impact}")

        if plan.reasoning:
            print(f"\n{_dim('Reasoning (hidden from user by default):')}")
            print(_dim(plan.reasoning[:500] + ("..." if len(plan.reasoning) > 500 else "")))

        # 7. Approval state
        if isinstance(proposal, ProposalPending):
            print(f"\n{_bold('Approval:')} pending (id={proposal.approval_id})")
            print(_dim("  → Mike clicks Approve..."))
            decision = gate.decide(
                approval_id=proposal.approval_id,
                decision=Decision.APPROVE,
                decided_by_founder_id=founder.id,
            )
            print(_green(f"  ✓ approved. envelope counter: {decision.envelope.consecutive_approvals}/{decision.envelope.threshold}"))
        elif isinstance(proposal, ProposalAccepted):
            print(f"\n{_bold('Approval:')} auto-executed (envelope already AUTO)")
        elif isinstance(proposal, ProposalDenied):
            print(f"\n{_bold('Approval:')} denied — {proposal.reason}")

        # 8. Audit + cost
        costs = session.exec(select(Cost).where(Cost.business_id == business.id)).all()
        total_cost = sum((c.cost_usd for c in costs), Decimal("0"))
        print(f"\n{_bold('Cost:')} ${total_cost} ({len(costs)} call(s))")
        if total_cost == 0:
            print(_dim("  (Ollama Cloud subscription — no per-token charge)"))

        events = session.exec(
            select(Activity).where(Activity.business_id == business.id)
        ).all()
        print(f"\n{_bold('Activity log:')} {len(events)} events")
        for e in events:
            print(f"  - {e.event_type}")

        print(f"\n{_bold(_green('Done.'))}\n")


if __name__ == "__main__":
    asyncio.run(main())
