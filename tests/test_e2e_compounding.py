"""End-to-end probe for the compounding-value flow.

Day-zero onboarding has its own e2e (`test_e2e_onboarding.py`).
This probe picks up after that: an existing business with a
hired CEO, talking to the cofounder about ongoing work, and
verifies the compounding artifacts actually materialize:

  * CEO.propose() → kanban BACKLOG cards (one per Plan task,
    role tag → owner_role)
  * Workforce.dispatch() → cards advance BACKLOG → IN_PROGRESS →
    REVIEW with evidence (or release back to READY on blocked)
  * memory.note → bounded MEMORY/USER blocks → auto-injected
    into the NEXT session's CEO system prompt

If this test breaks, the "your AI cofounder learns + compounds
weekly" pitch is broken and the kanban/memory shipping was for
nothing. This is the regression net for the whole compounding
loop.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlmodel import Session, select

from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.cofounder.ceo import CEO, Plan
from korpha.cofounder.director import (
    AttemptResult, DEFAULT_PERSONALITIES, DirectorPersonality,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.cofounder.workforce import Workforce
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.providers.mock import MockProvider
from korpha.inference.types import CompletionRequest, CompletionResponse
from korpha.kanban import KanbanBoard
from korpha.kanban.model import KanbanCard, KanbanColumn


_PLAN_RESPONSE = (
    '{"summary":"Ship the pricing page + write 3 LinkedIn posts",'
    '"rationale":["pricing first","then awareness","then traffic"],'
    '"next_action":"deploy /pricing route",'
    '"tasks":['
    '"[CTO] deploy the /pricing route with Stripe checkout",'
    '"[CMO] write 3 LinkedIn posts about the launch",'
    '"[COO] update support docs with the new tier"'
    '],'
    '"estimated_hours":6,"expected_impact":"first paying customer"}'
)


class _ScriptedPool:
    """Minimal InferencePool stand-in. Returns a single canned
    response on every call so we can drive CEO.propose() without
    a real provider."""

    def __init__(self, response_text: str) -> None:
        self._text = response_text
        self.calls: list[CompletionRequest] = []

    async def complete(
        self, request: CompletionRequest, *, account=None,
    ) -> CompletionResponse:
        self.calls.append(request)
        from decimal import Decimal
        return CompletionResponse(
            content=self._text,
            tool_calls=(),
            input_tokens=10, output_tokens=200, cached_tokens=0,
            cost_usd=Decimal("0.001"),
            provider="mock", model="mock-pro", account_id="test",
            reasoning=None,
        )


class _StubCostTracker:
    """Wraps the scripted pool. Implements the small surface
    ``CEO.propose()`` actually uses."""

    def __init__(self, pool: _ScriptedPool) -> None:
        self.pool = pool

    async def complete(
        self, request, *, session=None, business_id=None,
        agent_role_id=None, thread_id=None,
    ):
        return await self.pool.complete(request)


class _StubDirector:
    """Director that returns a scripted AttemptResult — no LLM."""

    def __init__(
        self, personality: DirectorPersonality, session: Session,
        result: AttemptResult,
    ) -> None:
        self.personality = personality
        self.session = session
        self._result = result

    async def attempt(
        self, *, business: Business, founder: Founder, task: str,
    ) -> AttemptResult:
        # Tag the title with the original task so we can correlate
        # to the kanban card after dispatch.
        from dataclasses import replace
        return replace(self._result, title=task[:60])


@pytest.fixture
def hired_business(session: Session, business: Business, founder: Founder):
    """Hire CTO + CMO + COO so workforce kanban claims work."""
    hiring = HiringService(session)
    hiring.ensure_ceo(business.id)
    for role in (RoleType.CTO, RoleType.CMO, RoleType.COO):
        agent = AgentRole(
            business_id=business.id, role_type=role,
            title=role.value.upper(),
        )
        session.add(agent)
    session.commit()
    return business


# ---- Plan → kanban mirror (#180) ----


@pytest.mark.asyncio
async def test_propose_lands_plan_tasks_on_kanban(
    session: Session, hired_business: Business, founder: Founder,
) -> None:
    """CEO.propose() with a 3-task plan → 3 BACKLOG cards, each
    tagged with the right owner_role."""
    from korpha.approvals.gate import ApprovalGate

    pool = _ScriptedPool(_PLAN_RESPONSE)
    ceo = CEO(
        session=session,
        cost_tracker=_StubCostTracker(pool),  # type: ignore[arg-type]
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    plan, _proposal = await ceo.propose(
        business=hired_business, founder=founder,
        founder_input="ship the pricing page and announce it",
    )
    assert len(plan.tasks) == 3

    cards = list(session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == hired_business.id)
    ).all())
    assert len(cards) == 3
    by_owner = {c.owner_role: c for c in cards}
    assert "cto" in by_owner
    assert "cmo" in by_owner
    assert "coo" in by_owner
    # All in BACKLOG
    for c in cards:
        assert c.column == KanbanColumn.BACKLOG
    # Title has the role tag stripped
    cto_card = by_owner["cto"]
    assert "[CTO]" not in cto_card.title
    assert "deploy the /pricing route" in cto_card.title


# ---- Workforce → kanban (#183) ----


@pytest.mark.asyncio
async def test_workforce_dispatch_advances_cards_through_columns(
    session: Session, hired_business: Business, founder: Founder,
) -> None:
    """End-to-end: propose → kanban BACKLOG → workforce dispatch →
    cards land in REVIEW with evidence attached."""
    from korpha.approvals.gate import ApprovalGate

    # 1. Produce the plan (which mirrors to BACKLOG).
    pool = _ScriptedPool(_PLAN_RESPONSE)
    ceo = CEO(
        session=session,
        cost_tracker=_StubCostTracker(pool),  # type: ignore[arg-type]
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    plan, _ = await ceo.propose(
        business=hired_business, founder=founder,
        founder_input="ship the pricing page",
    )

    # 2. Build a workforce with stub directors.
    shipped = AttemptResult(
        role_type=RoleType.CTO,
        title="placeholder",
        status="shipped",
        summary="deployed at https://example.com/pricing",
        detail="merged PR #42",
        blocker_ids=[], raw_response="",
        reasoning=None, cost_usd=0.0,
    )
    directors = {
        role: _StubDirector(
            personality=DEFAULT_PERSONALITIES[role],
            session=session,
            result=AttemptResult(
                role_type=role, title="placeholder",
                status="shipped",
                summary=f"{role.value.upper()} done at https://example.com",
                detail=f"{role.value.upper()} evidence",
                blocker_ids=[], raw_response="",
                reasoning=None, cost_usd=0.0,
            ),
        ) for role in (RoleType.CTO, RoleType.CMO, RoleType.COO)
    }
    workforce = Workforce(directors=directors)  # type: ignore[arg-type]

    # 3. Dispatch the plan tasks.
    results = await workforce.dispatch(
        business=hired_business, founder=founder, tasks=plan.tasks,
    )
    assert len(results) == 3
    assert all(r.status == "shipped" for r in results)

    # 4. All cards now in REVIEW with evidence.
    cards = list(session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == hired_business.id)
    ).all())
    assert len(cards) == 3
    for c in cards:
        assert c.column == KanbanColumn.REVIEW, (
            f"card {c.title!r} stuck in {c.column.value}"
        )
        assert c.review_evidence is not None
        assert "https://" in c.review_evidence


@pytest.mark.asyncio
async def test_blocked_director_returns_card_to_ready(
    session: Session, hired_business: Business, founder: Founder,
) -> None:
    """A blocked attempt releases the claim back to READY rather
    than parking the card in IN_PROGRESS forever."""
    from korpha.approvals.gate import ApprovalGate

    pool = _ScriptedPool(_PLAN_RESPONSE)
    ceo = CEO(
        session=session,
        cost_tracker=_StubCostTracker(pool),  # type: ignore[arg-type]
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    plan, _ = await ceo.propose(
        business=hired_business, founder=founder,
        founder_input="x",
    )

    blocked = AttemptResult(
        role_type=RoleType.CTO, title="placeholder",
        status="blocked",
        summary="needs founder decision on pricing tiers",
        detail=None, blocker_ids=[], raw_response="",
        reasoning=None, cost_usd=0.0,
    )
    directors = {
        role: _StubDirector(
            personality=DEFAULT_PERSONALITIES[role],
            session=session, result=blocked,
        ) for role in (RoleType.CTO, RoleType.CMO, RoleType.COO)
    }
    workforce = Workforce(directors=directors)  # type: ignore[arg-type]
    await workforce.dispatch(
        business=hired_business, founder=founder, tasks=plan.tasks,
    )

    cards = list(session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == hired_business.id)
    ).all())
    for c in cards:
        # Blocked → claim released → back in READY
        assert c.column == KanbanColumn.READY
        assert c.claimed_by_agent_role_id is None


# ---- Memory carries across sessions (#192) ----


@pytest.mark.asyncio
async def test_memory_blocks_appear_in_next_session_system_prompt(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Save a USER preference in session 1; verify it appears in
    session 2's CEO system prompt automatically (no recall call)."""
    from korpha.approvals.gate import ApprovalGate
    from korpha.memory.notes import FounderNoteService
    from korpha.skills.types import SkillContext

    HiringService(session).ensure_ceo(business.id)

    # Session 1: save a preference via the agent skill (proxy for
    # what the LLM would do mid-conversation).
    from korpha.skills import default_registry
    skill = default_registry.skills["memory.note"]
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=_StubCostTracker(_ScriptedPool("x")),  # type: ignore[arg-type]
    )
    await skill.run(ctx=ctx, args={
        "action": "add", "store": "user",
        "content": "Mike speaks German natively, prefers concise replies",
    })
    await skill.run(ctx=ctx, args={
        "action": "add", "store": "memory",
        "content": "Project name is WidgetCo, Stripe webhook key rotates monthly",
    })

    # Session 2: build a fresh CEO + ask it to assemble messages.
    # The bounded notes should land in the system prompt without
    # any explicit recall.
    pool = _ScriptedPool(_PLAN_RESPONSE)
    ceo = CEO(
        session=session,
        cost_tracker=_StubCostTracker(pool),  # type: ignore[arg-type]
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    msgs = ceo._build_messages(
        business=business, founder=founder, history=[],
        user_message="what should I work on this week?",
    )
    system = msgs[0].content
    assert "USER PROFILE" in system
    assert "AGENT MEMORY" in system
    assert "Mike speaks German" in system
    assert "Stripe webhook" in system


@pytest.mark.asyncio
async def test_memory_isolates_across_businesses(
    session: Session, founder: Founder,
) -> None:
    """Memory saved against business A doesn't leak into
    business B's system prompt — each business gets its own
    cofounder identity."""
    from korpha.approvals.gate import ApprovalGate
    from korpha.memory.notes import FounderNoteService

    biz_a = Business(
        founder_id=founder.id, name="A", description="",
    )
    biz_b = Business(
        founder_id=founder.id, name="B", description="",
    )
    session.add_all([biz_a, biz_b]); session.commit()
    session.refresh(biz_a); session.refresh(biz_b)
    HiringService(session).ensure_ceo(biz_a.id)
    HiringService(session).ensure_ceo(biz_b.id)

    svc = FounderNoteService(session)
    svc.add(
        business_id=biz_a.id, founder_id=founder.id,
        store="memory", content="A's secret stuff",
    )

    pool = _ScriptedPool("x")
    ceo = CEO(
        session=session,
        cost_tracker=_StubCostTracker(pool),  # type: ignore[arg-type]
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    msgs_b = ceo._build_messages(
        business=biz_b, founder=founder, history=[],
        user_message="hi",
    )
    assert "A's secret stuff" not in msgs_b[0].content


# ---- The full compounding loop ----


@pytest.mark.asyncio
async def test_full_compounding_loop_propose_dispatch_review(
    session: Session, hired_business: Business, founder: Founder,
) -> None:
    """The headline integration test. Drives the full cycle:
       propose → kanban → workforce → REVIEW
    and asserts the artifacts the user sees on /app/kanban,
    /app/weekly, and /app/dashboard are in the right state."""
    from korpha.approvals.gate import ApprovalGate

    pool = _ScriptedPool(_PLAN_RESPONSE)
    ceo = CEO(
        session=session,
        cost_tracker=_StubCostTracker(pool),  # type: ignore[arg-type]
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    plan, proposal = await ceo.propose(
        business=hired_business, founder=founder,
        founder_input="time to ship",
    )

    # Plan exists + Approval staged
    assert plan.summary
    assert proposal is not None

    # Kanban has 3 BACKLOG cards
    board = KanbanBoard(session)
    snapshot = board.board_snapshot(hired_business.id)
    assert len(snapshot[KanbanColumn.BACKLOG]) == 3

    # Dispatch (with mock directors) advances them
    directors = {
        role: _StubDirector(
            personality=DEFAULT_PERSONALITIES[role],
            session=session,
            result=AttemptResult(
                role_type=role, title="x", status="shipped",
                summary=f"{role.value} done at https://example.com",
                detail=None, blocker_ids=[], raw_response="",
                reasoning=None, cost_usd=0.0,
            ),
        ) for role in (RoleType.CTO, RoleType.CMO, RoleType.COO)
    }
    workforce = Workforce(directors=directors)  # type: ignore[arg-type]
    await workforce.dispatch(
        business=hired_business, founder=founder, tasks=plan.tasks,
    )

    # Final state: 3 cards in REVIEW, BACKLOG drained, evidence
    # attached on each.
    snapshot = board.board_snapshot(hired_business.id)
    assert len(snapshot[KanbanColumn.BACKLOG]) == 0
    assert len(snapshot[KanbanColumn.IN_PROGRESS]) == 0
    assert len(snapshot[KanbanColumn.REVIEW]) == 3
    for c in snapshot[KanbanColumn.REVIEW]:
        assert c.review_evidence is not None
        assert c.review_evidence.startswith(("https://", "http://")) or (
            "https://" in (c.review_evidence or "")
        )

    # Mike's weekly view would show 0 shipped (still in REVIEW)
    # but 3 awaiting review — that's the gate working as designed.
