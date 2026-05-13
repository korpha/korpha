"""Live-API end-to-end probe for the compounding loop.

The stub-driven ``test_e2e_compounding.py`` proves the wiring;
this proves the *prompts* actually work — that DeepSeek V4 (or
whatever model is behind the configured provider) produces JSON
the CEO + skills can parse, that the system prompt with bounded
MEMORY/USER blocks doesn't confuse the model, and that the
kanban mirror lands the right cards.

Skipped automatically when no provider key is configured. Run
with::

    OPENCODE_API_KEY=... pytest tests/integration -m integration
    OLLAMA_CLOUD_API_KEY=... pytest tests/integration -m integration

Token-conscious: one CEO.propose() + one memory.note round-trip.
On Workhorse tier this is sub-cent.
"""
from __future__ import annotations

import os
from uuid import UUID

import pytest
from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine, select

from korpha.approvals.gate import ApprovalGate
from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.cofounder.ceo import CEO
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import RoleType
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    ProviderAccount,
    ollama_cloud_provider,
    opencode_go_provider,
)
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType
from korpha.kanban.model import KanbanCard, KanbanColumn
from korpha.memory.notes import FounderNoteService
from korpha.skills import default_registry
from korpha.skills.types import SkillContext

load_dotenv()

pytestmark = pytest.mark.integration


OPENCODE_KEY = os.getenv("OPENCODE_API_KEY")
OLLAMA_KEY = os.getenv("OLLAMA_CLOUD_API_KEY")
ANY_KEY = OPENCODE_KEY or OLLAMA_KEY
SKIP_REASON = "neither OPENCODE_API_KEY nor OLLAMA_CLOUD_API_KEY set"


def _build_pool() -> tuple[InferencePool, ProviderAccount]:
    """Pick whichever provider key is configured. Prefer OpenCode
    Go (cheaper subscription tier); fall back to Ollama Cloud."""
    if OPENCODE_KEY:
        provider = opencode_go_provider()
        account = ProviderAccount(
            provider_name="opencode-go",
            auth_type=AuthType.API_KEY,
            tier_models={
                InferenceTier.WORKHORSE: "deepseek-v4-flash",
                InferenceTier.PRO: "deepseek-v4-pro",
            },
            api_key=OPENCODE_KEY,
            label="opencode-go",
        )
    else:
        assert OLLAMA_KEY is not None
        provider = ollama_cloud_provider()
        account = ProviderAccount(
            provider_name="ollama-cloud",
            auth_type=AuthType.API_KEY,
            tier_models={
                InferenceTier.WORKHORSE: "deepseek-v4-flash:cloud",
                InferenceTier.PRO: "deepseek-v4-pro:cloud",
            },
            api_key=OLLAMA_KEY,
            label="ollama-cloud-1",
        )
    pool = InferencePool(providers=[provider], accounts=[account])
    return pool, account


@pytest.fixture
def live_session(tmp_path):
    """Real DB + seeded business + hired CEO/CTO/CMO/COO. Each
    test gets a fresh DB so they don't interfere."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/live.db",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="mike@example.com", display_name="Mike")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="Cofounder Test Co",
            description="indie SaaS for solo Python devs",
        )
        s.add(b); s.commit(); s.refresh(b)
        hiring = HiringService(s)
        hiring.ensure_ceo(b.id)
        for role in (RoleType.CTO, RoleType.CMO, RoleType.COO):
            from korpha.cofounder.model import AgentRole
            s.add(AgentRole(
                business_id=b.id, role_type=role,
                title=role.value.upper(),
            ))
        s.commit()
        yield s, f, b


@pytest.mark.asyncio
@pytest.mark.skipif(ANY_KEY is None, reason=SKIP_REASON)
async def test_live_propose_lands_kanban_cards(live_session) -> None:
    """The headline contract: CEO.propose() against a real LLM
    must produce parseable Plan JSON, and the role-tagged tasks
    must mirror to BACKLOG cards on /app/kanban.

    If this test fails on a real model, the prompt is broken and
    the whole compounding loop falls apart in production. That's
    the regression we're guarding against."""
    session, founder, business = live_session
    pool, _account = _build_pool()
    cost_tracker = CostTracker(pool=pool)

    ceo = CEO(
        session=session,
        cost_tracker=cost_tracker,
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )

    plan, proposal = await ceo.propose(
        business=business,
        founder=founder,
        founder_input=(
            "I want to ship a pricing page for our Python "
            "deployment tool and announce it. Give me a tight "
            "plan with one task per role (CTO/CMO/COO)."
        ),
    )

    # Plan parsed cleanly
    assert plan.summary, "model returned an empty plan summary"
    assert plan.tasks, "model returned no tasks"
    # Every task should carry a role tag (CEO prompt enforces this).
    # Tolerate a missing tag on at most one — sometimes the model
    # phrases tasks ambiguously.
    tagged = sum(
        1 for t in plan.tasks
        if t.lstrip().startswith(("[CTO", "[CMO", "[COO"))
    )
    assert tagged >= len(plan.tasks) - 1, (
        f"only {tagged}/{len(plan.tasks)} tasks tagged with a role: "
        f"{plan.tasks!r}"
    )

    # Approval staged
    assert proposal is not None

    # Kanban mirror landed cards in BACKLOG
    cards = list(session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).all())
    assert len(cards) >= 1, "no kanban cards created from plan"
    for c in cards:
        assert c.column == KanbanColumn.BACKLOG
        assert "[CTO" not in c.title and "[CMO" not in c.title, (
            "role tag should have been stripped from card title"
        )


@pytest.mark.asyncio
@pytest.mark.skipif(ANY_KEY is None, reason=SKIP_REASON)
async def test_live_memory_blocks_inject_into_system_prompt(
    live_session,
) -> None:
    """Save a fact via the memory.note skill, then build a fresh
    CEO and verify the bounded MEMORY/USER block lands in the
    system prompt with the saved fact verbatim. This is the
    Hermes self-improvement pitch end-to-end against a real
    provider's tokenizer."""
    session, founder, business = live_session
    pool, _account = _build_pool()
    cost_tracker = CostTracker(pool=pool)

    skill = default_registry.skills["memory.note"]
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=cost_tracker,
    )
    await skill.run(ctx=ctx, args={
        "action": "add", "store": "user",
        "content": (
            "Mike speaks German natively, prefers concise replies, "
            "knows Python + B2B SaaS"
        ),
    })

    # Now build messages as if a fresh session is starting.
    ceo = CEO(
        session=session,
        cost_tracker=cost_tracker,
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    msgs = ceo._build_messages(
        business=business, founder=founder, history=[],
        user_message="what's the plan today?",
    )
    system = msgs[0].content
    assert "USER PROFILE" in system
    assert "Mike speaks German" in system
    # Char-count header rendered
    assert "%" in system

    # Bounded note actually persisted (round-trip via the service)
    rows = FounderNoteService(session).list(
        business_id=business.id, founder_id=founder.id, store="user",
    )
    assert any("German" in r.content for r in rows)


@pytest.mark.asyncio
@pytest.mark.skipif(ANY_KEY is None, reason=SKIP_REASON)
async def test_live_propose_then_recall_carries_across(
    live_session,
) -> None:
    """The complete compounding contract: session 1 saves a fact;
    session 2's CEO.propose() actually USES it (we ship a fact the
    LLM should weave into its plan, then check the response cites
    it). Probabilistic but a strong-enough signal that the model
    sees the system prompt — if this fails repeatedly the bounded
    block is being dropped somewhere in the pipeline."""
    session, founder, business = live_session
    pool, _account = _build_pool()
    cost_tracker = CostTracker(pool=pool)

    skill = default_registry.skills["memory.note"]
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=cost_tracker,
    )
    # A specific, easy-to-verify fact the model should mention.
    await skill.run(ctx=ctx, args={
        "action": "add", "store": "memory",
        "content": (
            "We use Stripe for payments. Webhook secret rotates "
            "monthly on the 15th."
        ),
    })

    ceo = CEO(
        session=session,
        cost_tracker=cost_tracker,
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    plan, _ = await ceo.propose(
        business=business,
        founder=founder,
        founder_input=(
            "I'm worried about our payment setup. What should we "
            "review next month?"
        ),
    )
    haystack = " ".join([
        plan.summary or "",
        plan.next_action or "",
        " ".join(plan.tasks),
        " ".join(plan.rationale),
        plan.raw_response or "",
    ]).lower()
    # Stripe was in MEMORY → at minimum the model has it in context
    # and almost certainly references it given the prompt pulls
    # toward the same topic. We accept either "stripe" or "webhook"
    # as evidence the bounded block reached the model.
    assert "stripe" in haystack or "webhook" in haystack, (
        f"model never referenced Stripe/webhook despite it being in "
        f"MEMORY. Plan summary: {plan.summary!r}"
    )
