"""PR-INT-13/14/15 tests — VP agent runner + hr.delegate_to_vp +
workforce honors business_unit_id.

Uses a scripted Provider that returns different content based on
which prompt the agent is currently issuing (router vs synth) so
the full router → skill → synth loop is exercised end-to-end
without a real LLM."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from korpha.audit.model import Activity, ActorType, InferenceTier
from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.cofounder.vp_runner import run_vp_turn
from korpha.identity.model import Founder
from korpha.inference import (
    CompletionRequest, ProviderAccount, TierPricing,
)
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.inference.provider import Provider
from korpha.inference.providers.mock import MockProvider
from korpha.inference.registry import AuthType
from korpha.inference.types import CompletionResponse
from korpha.memory.model import LongTermMemoryEntry
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


@dataclass
class ScriptedProvider(Provider):
    """Returns canned content based on substring matches in the prompt.

    Items earlier in ``rules`` win. Each rule is (substring_to_match,
    response_text). Falls back to ``default`` when nothing matches."""

    name: str = "scripted"
    rules: list[tuple[str, str]] = field(default_factory=list)
    default: str = "(scripted fallback)"
    calls: list[str] = field(default_factory=list)

    async def complete(
        self, request: CompletionRequest, account: ProviderAccount,
    ) -> CompletionResponse:
        text = "\n".join(m.content for m in request.messages)
        self.calls.append(text[:300])
        content = self.default
        for needle, response in self.rules:
            if needle in text:
                content = response
                break
        return CompletionResponse(
            content=content,
            tool_calls=(),
            input_tokens=max(1, len(text) // 4),
            output_tokens=max(1, len(content) // 4),
            cached_tokens=0,
            cost_usd=Decimal("0"),
            provider=self.name,
            model=account.tier_models.get(request.tier, "scripted-default"),
            account_id=str(account.id),
            cache_hit_ratio=0.0,
        )


def _account() -> ProviderAccount:
    return ProviderAccount(
        provider_name="scripted",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "scripted-workhorse",
            InferenceTier.PRO: "scripted-pro",
        },
        pricing={},
        api_key="sk-test",
    )


def _make_tracker(provider: ScriptedProvider) -> CostTracker:
    return CostTracker(pool=InferencePool(
        providers=[provider], accounts=[_account()],
    ))


@pytest.fixture
def tree(session: Session, business: Business) -> dict[str, BusinessUnit]:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    kdp = board.create(
        business_id=business.id, name="Romance KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    pod = board.create(
        business_id=business.id, name="Merch POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    # PR-INT-1 auto-hires VPs; if board.create didn't (depends on path),
    # set owners now to make tests deterministic.
    if kdp.owner_agent_role_id is None:
        hiring = HiringService(session)
        vp = hiring.hire(
            business_id=business.id, role_type=RoleType.WORKER,
            title="Line VP: KDP", specialty="kdp-line-vp",
            business_unit_id=kdp.id,
        )
        kdp.owner_agent_role_id = vp.id
        session.add(kdp); session.commit()
    if pod.owner_agent_role_id is None:
        hiring = HiringService(session)
        vp = hiring.hire(
            business_id=business.id, role_type=RoleType.WORKER,
            title="Line VP: POD", specialty="pod-line-vp",
            business_unit_id=pod.id,
        )
        pod.owner_agent_role_id = vp.id
        session.add(pod); session.commit()
    session.refresh(kdp); session.refresh(pod)
    return {"root": root, "kdp": kdp, "pod": pod}


# ---------------------------------------------------------------------------
# run_vp_turn — direct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vp_turn_runs_skill_in_unit_namespace(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Scripted router picks memory.remember; the VP turn must
    auto-stamp the entry with KDP's namespace (NOT None)."""
    provider = ScriptedProvider(rules=[
        # Router decision: call memory.remember
        ("Available skills:", (
            '{"action":"use_skill","skill_name":"memory.remember",'
            '"skill_args":{"text":"Highland Rogue book 4 themes locked",'
            '"tags":"kdp,launch"}}'
        )),
        # Synth: VP composes its confirmation
        ("You just ran the skill", "Done — themes captured in KDP memory."),
    ], default="Stored.")

    result = await run_vp_turn(
        session=session, business=business, founder=founder,
        unit_id=tree["kdp"].id,
        task="Note that Highland Rogue book 4 themes are locked.",
        cost_tracker=_make_tracker(provider),
    )

    assert result.unit_id == tree["kdp"].id
    assert result.unit_name == "Romance KDP"
    assert any(s.skill_name == "memory.remember" for s in result.skills_used)
    # The actual proof: memory entry has KDP's namespace
    entries = list(session.exec(select(LongTermMemoryEntry)).all())
    assert len(entries) == 1
    assert entries[0].namespace_id == tree["kdp"].memory_namespace_id


@pytest.mark.asyncio
async def test_vp_turn_writes_activity_log(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    provider = ScriptedProvider(rules=[
        ("Available skills:", '{"action":"respond","content":"acked"}'),
    ])
    await run_vp_turn(
        session=session, business=business, founder=founder,
        unit_id=tree["kdp"].id, task="ping",
        cost_tracker=_make_tracker(provider),
    )
    acts = list(session.exec(
        select(Activity).where(Activity.event_type == "vp.task_handled")
    ).all())
    assert len(acts) == 1
    assert acts[0].business_unit_id == tree["kdp"].id
    assert acts[0].actor_type == ActorType.AGENT
    assert acts[0].actor_id == tree["kdp"].owner_agent_role_id


@pytest.mark.asyncio
async def test_vp_turn_direct_reply_no_skill(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """When the router returns action=respond, no skill runs."""
    provider = ScriptedProvider(rules=[
        ("Available skills:", (
            '{"action":"respond","content":"Nothing actionable — '
            'forwarding to founder."}'
        )),
    ])
    result = await run_vp_turn(
        session=session, business=business, founder=founder,
        unit_id=tree["kdp"].id, task="just say hi",
        cost_tracker=_make_tracker(provider),
    )
    assert result.skills_used == []
    assert "Nothing actionable" in result.content


@pytest.mark.asyncio
async def test_vp_turn_unknown_unit_raises(
    session: Session, business: Business, founder: Founder,
) -> None:
    provider = ScriptedProvider()
    with pytest.raises(SkillError, match="BusinessUnit"):
        await run_vp_turn(
            session=session, business=business, founder=founder,
            unit_id=uuid4(), task="x",
            cost_tracker=_make_tracker(provider),
        )


@pytest.mark.asyncio
async def test_vp_turn_unit_from_other_business_rejected(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    other = Business(
        founder_id=founder.id, name="OtherCo",
        description="x", founder_brief={},
    )
    session.add(other); session.commit(); session.refresh(other)
    provider = ScriptedProvider()
    with pytest.raises(SkillError, match="doesn't belong"):
        await run_vp_turn(
            session=session, business=other, founder=founder,
            unit_id=tree["kdp"].id, task="x",
            cost_tracker=_make_tracker(provider),
        )


@pytest.mark.asyncio
async def test_vp_turn_unowned_unit_still_runs(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """A unit with no owner_agent_role_id can still run a turn
    (cost gets attributed to no agent)."""
    # Wipe owner
    tree["pod"].owner_agent_role_id = None
    session.add(tree["pod"]); session.commit()
    provider = ScriptedProvider(rules=[
        ("Available skills:", '{"action":"respond","content":"ack"}'),
    ])
    result = await run_vp_turn(
        session=session, business=business, founder=founder,
        unit_id=tree["pod"].id, task="ping",
        cost_tracker=_make_tracker(provider),
    )
    assert result.vp_agent_role_id is None
    assert result.unit_name == "Merch POD"


# ---------------------------------------------------------------------------
# hr.delegate_to_vp skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_to_vp_skill_runs_vp_turn(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """CEO calls hr.delegate_to_vp(unit='Romance KDP', task='...') →
    VP turn runs → memory entry lands in KDP namespace."""
    provider = ScriptedProvider(rules=[
        ("Available skills:", (
            '{"action":"use_skill","skill_name":"memory.remember",'
            '"skill_args":{"text":"book 4 themes locked","tags":"kdp"}}'
        )),
        ("You just ran the skill", "Themes captured."),
    ])
    tracker = _make_tracker(provider)
    ceo_ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=tracker, business_unit_id=None,  # CEO at root
    )
    skill = default_registry.skills["hr.delegate_to_vp"]
    out = await skill.run(
        ctx=ceo_ctx,
        args={
            "unit": "Romance KDP",
            "task": "Note that book 4 themes are locked.",
        },
    )
    assert out.payload["unit_name"] == "Romance KDP"
    assert out.payload["unit_id"] == str(tree["kdp"].id)
    assert "memory.remember" in out.payload["skills_used_by_vp"]
    # The actual proof: memory landed in KDP namespace, NOT None
    entries = list(session.exec(select(LongTermMemoryEntry)).all())
    assert len(entries) == 1
    assert entries[0].namespace_id == tree["kdp"].memory_namespace_id


@pytest.mark.asyncio
async def test_delegate_unknown_unit_errors_helpfully(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    provider = ScriptedProvider()
    tracker = _make_tracker(provider)
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=tracker,
    )
    skill = default_registry.skills["hr.delegate_to_vp"]
    with pytest.raises(SkillError, match="No BusinessUnit"):
        await skill.run(
            ctx=ctx, args={"unit": "Bogus Line", "task": "x"},
        )


@pytest.mark.asyncio
async def test_delegate_missing_task_errors(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    provider = ScriptedProvider()
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=_make_tracker(provider),
    )
    skill = default_registry.skills["hr.delegate_to_vp"]
    with pytest.raises(SkillError, match="task required"):
        await skill.run(ctx=ctx, args={"unit": "Romance KDP"})


@pytest.mark.asyncio
async def test_delegate_missing_unit_errors(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    provider = ScriptedProvider()
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=_make_tracker(provider),
    )
    skill = default_registry.skills["hr.delegate_to_vp"]
    with pytest.raises(SkillError, match="unit required"):
        await skill.run(ctx=ctx, args={"task": "x"})


@pytest.mark.asyncio
async def test_delegate_uuid_works_as_well(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """unit arg accepts UUID string in addition to friendly name."""
    provider = ScriptedProvider(rules=[
        ("Available skills:", '{"action":"respond","content":"ack"}'),
    ])
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=_make_tracker(provider),
    )
    skill = default_registry.skills["hr.delegate_to_vp"]
    out = await skill.run(
        ctx=ctx,
        args={"unit": str(tree["pod"].id), "task": "ping"},
    )
    assert out.payload["unit_name"] == "Merch POD"


# ---------------------------------------------------------------------------
# VP cross-unit cooperation auto-fires with unit context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vp_can_ask_about_without_from_unit_id(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """When KDP VP calls cooperation.ask_about without from_unit_id,
    the skill auto-defaults from_unit_id to KDP's id (because the
    VP's SkillContext.business_unit_id is set)."""
    provider = ScriptedProvider(rules=[
        ("Available skills:", (
            '{"action":"use_skill","skill_name":"cooperation.ask_about",'
            '"skill_args":{"to_unit_id":"Merch POD",'
            '"question":"capacity?"}}'
        )),
        ("You just ran the skill", "POD acknowledged."),
    ])
    result = await run_vp_turn(
        session=session, business=business, founder=founder,
        unit_id=tree["kdp"].id,
        task="Coordinate with POD about Highland Rogue merch.",
        cost_tracker=_make_tracker(provider),
    )
    # ask_about ran + audit log shows KDP→POD
    from korpha.cooperation.model import CrossUnitQueryLog
    logs = list(session.exec(select(CrossUnitQueryLog)).all())
    assert len(logs) == 1
    assert logs[0].from_unit_id == tree["kdp"].id
    assert logs[0].to_unit_id == tree["pod"].id
