"""CEO tests — uses MockProvider for deterministic completion responses."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sqlmodel import Session

from korpha.approvals.gate import ApprovalGate, ProposalPending
from korpha.approvals.model import ActionClass
from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.cofounder.ceo import CEO, _parse_plan
from korpha.cofounder.hiring import HiringService
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    MockProvider,
    ProviderAccount,
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
        },
        api_key="x",
    )


@dataclass
class _CEOFactory:
    session: Session
    pool: InferencePool

    def build(self) -> CEO:
        tracker = CostTracker(pool=self.pool)
        hiring = HiringService(self.session)
        gate = ApprovalGate(self.session)
        return CEO(
            session=self.session, cost_tracker=tracker, hiring=hiring, gate=gate
        )


@pytest.mark.asyncio
async def test_respond_returns_completion(
    session: Session, business: Business, founder: Founder
) -> None:
    pool = InferencePool(providers=[MockProvider()], accounts=[_account()])
    ceo = _CEOFactory(session=session, pool=pool).build()

    response = await ceo.respond(
        business=business,
        founder=founder,
        founder_message="What should I focus on this week?",
    )
    assert response.input_tokens > 0
    assert response.content


@pytest.mark.asyncio
async def test_propose_creates_pending_approval(
    session: Session, business: Business, founder: Founder
) -> None:
    plan_json = (
        '{"summary":"Ship a landing page","rationale":["fast feedback","low cost"],'
        '"next_action":"Draft copy","tasks":["Build the page","Draft a Twitter post"],'
        '"estimated_hours":4,"expected_impact":"Get 10 signups"}'
    )
    pool = InferencePool(
        providers=[MockProvider(static_response=plan_json)], accounts=[_account()]
    )
    ceo = _CEOFactory(session=session, pool=pool).build()

    plan, result = await ceo.propose(
        business=business,
        founder=founder,
        founder_input="I want $5k MRR. Plan this week.",
        action_class=ActionClass.INTERNAL,
    )

    assert plan.summary == "Ship a landing page"
    assert plan.rationale == ["fast feedback", "low cost"]
    assert plan.next_action == "Draft copy"
    assert plan.tasks == ["Build the page", "Draft a Twitter post"]
    assert plan.estimated_hours == 4.0
    assert plan.expected_impact == "Get 10 signups"
    assert isinstance(result, ProposalPending)


def test_parse_plan_handles_pure_json() -> None:
    plan = _parse_plan(
        '{"summary":"Test","rationale":["a"],"next_action":"go","estimated_hours":2,"expected_impact":"x"}',
        reasoning="thinking...",
    )
    assert plan.summary == "Test"
    assert plan.rationale == ["a"]
    assert plan.estimated_hours == 2.0
    assert plan.reasoning == "thinking..."


def test_parse_plan_handles_prose_around_json() -> None:
    plan = _parse_plan(
        'Here is the plan:\n{"summary":"S","rationale":["r"],"next_action":"a"}\n\nThanks!',
        reasoning=None,
    )
    assert plan.summary == "S"
    assert plan.next_action == "a"


def test_parse_plan_falls_back_when_no_json() -> None:
    plan = _parse_plan("Just some prose, no JSON here.", reasoning=None)
    assert "Just some prose" in plan.summary
    assert plan.requires_founder_approval


def test_parse_plan_handles_string_rationale() -> None:
    """Some models output rationale as a single string."""
    plan = _parse_plan(
        '{"summary":"x","rationale":"single reason","next_action":"y"}',
        reasoning=None,
    )
    assert plan.rationale == ["single reason"]


def test_parse_plan_handles_string_estimated_hours() -> None:
    plan = _parse_plan(
        '{"summary":"x","rationale":[],"next_action":"y","estimated_hours":"3.5"}',
        reasoning=None,
    )
    assert plan.estimated_hours == 3.5


def test_parse_plan_handles_garbage_estimated_hours() -> None:
    plan = _parse_plan(
        '{"summary":"x","rationale":[],"next_action":"y","estimated_hours":"about a day"}',
        reasoning=None,
    )
    assert plan.estimated_hours is None


def test_parse_plan_handles_markdown_code_fence() -> None:
    """Real models often wrap JSON in ```json ... ``` fences."""
    plan = _parse_plan(
        '```json\n{"summary":"S","rationale":["r"],"next_action":"a","tasks":["t1","t2"]}\n```',
        reasoning=None,
    )
    assert plan.summary == "S"
    assert plan.tasks == ["t1", "t2"]


def test_parse_plan_handles_prose_then_json_then_prose() -> None:
    plan = _parse_plan(
        'Here is the plan you requested:\n\n'
        '{"summary":"S","rationale":["r"],"next_action":"a","tasks":["t1"]}\n\n'
        'Let me know if you want changes.',
        reasoning=None,
    )
    assert plan.summary == "S"
    assert plan.tasks == ["t1"]


def test_parse_plan_handles_tasks_as_string() -> None:
    plan = _parse_plan(
        '{"summary":"x","rationale":[],"next_action":"y","tasks":"single task as string"}',
        reasoning=None,
    )
    assert plan.tasks == ["single task as string"]


def test_parse_plan_filters_empty_tasks() -> None:
    plan = _parse_plan(
        '{"summary":"x","rationale":[],"next_action":"y","tasks":["a","","   ","b"]}',
        reasoning=None,
    )
    assert plan.tasks == ["a", "b"]


def test_dispatch_tasks_falls_back_to_next_action() -> None:
    from korpha.cofounder.ceo import Plan

    plan = Plan(
        summary="x",
        rationale=[],
        next_action="do this",
        tasks=[],
        estimated_hours=None,
        expected_impact=None,
        requires_founder_approval=False,
        reasoning=None,
        raw_response="",
    )
    assert plan.dispatch_tasks() == ["do this"]


def test_dispatch_tasks_uses_tasks_when_present() -> None:
    from korpha.cofounder.ceo import Plan

    plan = Plan(
        summary="x",
        rationale=[],
        next_action="single",
        tasks=["a", "b", "c"],
        estimated_hours=None,
        expected_impact=None,
        requires_founder_approval=False,
        reasoning=None,
        raw_response="",
    )
    assert plan.dispatch_tasks() == ["a", "b", "c"]


# ---- CEO.handle (skill-aware) ----


@pytest.mark.asyncio
async def test_handle_direct_response_when_no_skill_fits(
    session: Session, business: Business, founder: Founder
) -> None:
    """Router returns action=respond → CEO.handle uses that content directly."""
    from korpha.skills.registry import SkillRegistry

    response_json = (
        '{"action":"respond","content":"Hi Mike! Quick check-in: nothing on '
        'fire right now. Want to brainstorm?"}'
    )
    pool = InferencePool(
        providers=[MockProvider(static_response=response_json)],
        accounts=[_account()],
    )
    tracker = CostTracker(pool=pool)
    hiring = HiringService(session)
    gate = ApprovalGate(session)
    ceo = CEO(
        session=session,
        cost_tracker=tracker,
        hiring=hiring,
        gate=gate,
        skills=SkillRegistry(),  # empty registry → handle short-circuits to respond
    )
    result = await ceo.handle(
        business=business,
        founder=founder,
        founder_message="just checking in",
    )
    assert result.skills_used == []
    assert result.content  # returned non-empty
    # router_response should be the same as final_response in the no-skill path
    assert result.router_response is result.final_response


@pytest.mark.asyncio
async def test_handle_routes_to_skill_when_decision_says_use_skill(
    session: Session, business: Business, founder: Founder
) -> None:
    """Router emits action=use_skill → skill runs → second LLM call synthesizes."""
    from korpha.skills.registry import SkillRegistry
    from korpha.skills.types import (
        Skill,
        SkillContext,
        SkillResult,
        SkillSpec,
    )

    class FakeSkill(Skill):
        spec = SkillSpec(
            name="test.fake",
            description="a fake skill for testing",
            parameters={"x": "anything"},
        )

        async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
            return SkillResult(
                skill_name=self.spec.name,
                summary="fake done",
                payload={"echoed": args},
                cost_usd=0.0,
            )

    registry = SkillRegistry()
    registry.add(FakeSkill())

    # MockProvider will return THIS for both LLM calls; that's fine for the
    # router (it parses action=use_skill) and for synthesis (we only check
    # skills_used and that the synth call happened).
    decision_json = (
        '{"action":"use_skill","skill_name":"test.fake","skill_args":{"x":"hello"}}'
    )
    pool = InferencePool(
        providers=[MockProvider(static_response=decision_json)],
        accounts=[_account()],
    )
    tracker = CostTracker(pool=pool)
    ceo = CEO(
        session=session,
        cost_tracker=tracker,
        hiring=HiringService(session),
        gate=ApprovalGate(session),
        skills=registry,
    )
    result = await ceo.handle(
        business=business,
        founder=founder,
        founder_message="please do the fake thing with x=hello",
    )
    assert len(result.skills_used) == 1
    assert result.skills_used[0].skill_name == "test.fake"
    assert result.skills_used[0].payload == {"echoed": {"x": "hello"}}


