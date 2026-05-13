"""PR-INT-6 tests — cooperation.ask_about synchronous dispatch.

Covers the integration layer that turns the v1 stub "queued" response
into a real in-process call: the target unit's owner agent answers
the question in ITS scoped memory namespace and the response is
captured on the CrossUnitQueryLog row.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

import korpha.cooperation.dispatch as dispatch_mod
from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.cooperation.dispatch import dispatch_ask_about
from korpha.cooperation.model import CrossUnitQueryLog
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.memory.model import LongTermMemoryEntry
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


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
    return {"root": root, "kdp": kdp, "pod": pod}


def _ctx(session, business, founder, unit_id=None) -> SkillContext:
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
        business_unit_id=unit_id,
    )


# ---------------------------------------------------------------------------
# Direct dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_returns_structured_response(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    response = await dispatch_ask_about(
        ctx=_ctx(session, business, founder, tree["kdp"].id),
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        question="any merch capacity?",
    )
    assert isinstance(response, dict)
    assert "answer" in response
    assert response["target_unit_id"] == str(tree["pod"].id)
    assert response["target_namespace_id"] == str(
        tree["pod"].memory_namespace_id
    )


@pytest.mark.asyncio
async def test_dispatch_unknown_target_unit(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Looking up a unit that doesn't exist returns a "not found"
    answer rather than crashing."""
    bogus = uuid4()
    response = await dispatch_ask_about(
        ctx=_ctx(session, business, founder, tree["kdp"].id),
        from_unit_id=tree["kdp"].id,
        to_unit_id=bogus,
        question="anyone home?",
    )
    assert "not found" in response["answer"]


@pytest.mark.asyncio
async def test_dispatch_filters_to_target_namespace(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Memory entries in OTHER namespaces never appear in the answer."""
    # Seed two entries: one in POD's namespace, one in KDP's namespace.
    in_target = LongTermMemoryEntry(
        business_id=business.id, founder_id=founder.id,
        text="POD has free capacity for romance covers next week",
        tags=["capacity"], score=0.9,
        namespace_id=tree["pod"].memory_namespace_id,
    )
    in_other = LongTermMemoryEntry(
        business_id=business.id, founder_id=founder.id,
        text="KDP scheduled a romance launch for friday",
        tags=["schedule"], score=0.9,
        namespace_id=tree["kdp"].memory_namespace_id,
    )
    session.add(in_target); session.add(in_other); session.commit()

    response = await dispatch_ask_about(
        ctx=_ctx(session, business, founder, tree["kdp"].id),
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        question="romance capacity",
    )
    memory_texts = " ".join(
        m["text"] for m in response["relevant_memories"]
    )
    # POD namespace entry MAY appear; KDP namespace entry MUST NOT.
    assert "KDP scheduled a romance launch" not in memory_texts


@pytest.mark.asyncio
async def test_dispatch_resolves_owner_agent(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """When the target has an owner_agent_role_id, the response
    carries that agent's id + title."""
    hiring = HiringService(session)
    owner = hiring.hire(
        business_id=business.id,
        role_type=RoleType.WORKER,
        title="Line VP: POD",
        specialty="POD operations",
        business_unit_id=tree["pod"].id,
    )
    tree["pod"].owner_agent_role_id = owner.id
    session.add(tree["pod"]); session.commit(); session.refresh(tree["pod"])

    response = await dispatch_ask_about(
        ctx=_ctx(session, business, founder, tree["kdp"].id),
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        question="any capacity?",
    )
    assert response["target_agent_role_id"] == str(owner.id)
    assert response["target_agent_title"] == "Line VP: POD"
    assert "Line VP: POD" in response["answer"]


@pytest.mark.asyncio
async def test_dispatch_unowned_target_still_answers(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Units without an owner agent still get a deterministic
    placeholder answer so the asker's flow doesn't crash."""
    response = await dispatch_ask_about(
        ctx=_ctx(session, business, founder, tree["kdp"].id),
        from_unit_id=tree["kdp"].id,
        to_unit_id=tree["pod"].id,
        question="anyone there?",
    )
    assert response["target_agent_role_id"] is None
    assert "(unowned unit)" in response["answer"]


@pytest.mark.asyncio
async def test_dispatch_llm_runner_override_used_when_set(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DISPATCH_LLM_RUNNER is set, dispatch routes through it."""
    captured = {}

    async def _fake_runner(**kwargs):
        captured.update(kwargs)
        return "LLM-driven answer from POD"

    monkeypatch.setattr(
        dispatch_mod, "DISPATCH_LLM_RUNNER", _fake_runner,
    )
    try:
        response = await dispatch_ask_about(
            ctx=_ctx(session, business, founder, tree["kdp"].id),
            from_unit_id=tree["kdp"].id,
            to_unit_id=tree["pod"].id,
            question="capacity?",
        )
    finally:
        monkeypatch.setattr(
            dispatch_mod, "DISPATCH_LLM_RUNNER", None,
        )

    assert response["answer"] == "LLM-driven answer from POD"
    assert captured["question"] == "capacity?"
    # Runner sees the target unit (so its system prompt can be
    # personalized) and the memory hits.
    assert captured["target_unit"].id == tree["pod"].id


@pytest.mark.asyncio
async def test_dispatch_llm_runner_exception_falls_back(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM runner throws, dispatch falls back to the stub
    answer rather than propagating the error to the asker."""
    async def _broken_runner(**kwargs):
        raise RuntimeError("LLM provider down")
    monkeypatch.setattr(
        dispatch_mod, "DISPATCH_LLM_RUNNER", _broken_runner,
    )
    try:
        response = await dispatch_ask_about(
            ctx=_ctx(session, business, founder, tree["kdp"].id),
            from_unit_id=tree["kdp"].id,
            to_unit_id=tree["pod"].id,
            question="anyone?",
        )
    finally:
        monkeypatch.setattr(
            dispatch_mod, "DISPATCH_LLM_RUNNER", None,
        )
    # Falls back — answer is the stub, not an exception
    assert isinstance(response["answer"], str)
    assert "anyone" in response["answer"].lower() or "POD" in response["answer"]


# ---------------------------------------------------------------------------
# Skill integration — ask_about now writes response_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_about_skill_writes_response_summary(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """After PR-INT-6, ask_about's CrossUnitQueryLog row carries the
    dispatcher's response_summary."""
    skill = default_registry.skills["cooperation.ask_about"]
    out = await skill.run(
        ctx=_ctx(session, business, founder, tree["kdp"].id),
        args={
            "from_unit_id": str(tree["kdp"].id),
            "to_unit_id": str(tree["pod"].id),
            "question": "Highland Rogue merch capacity?",
        },
    )
    assert out.payload["status"] == "answered"
    assert "response" in out.payload
    assert out.payload["response"]["target_unit_id"] == str(
        tree["pod"].id
    )

    logs = list(session.exec(select(CrossUnitQueryLog)).all())
    assert len(logs) == 1
    assert logs[0].response_summary  # populated
    assert len(logs[0].response_summary) <= 200


@pytest.mark.asyncio
async def test_ask_about_skill_blocks_cross_tree(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Cross-tree query without a grant still raises BEFORE dispatch
    runs — the cooperation board's authorization gate fires first.

    Cross-tree pair: a leaf under KDP vs a leaf under POD — they
    share no ancestor besides the root and are not siblings.
    """
    board = BusinessUnitBoard(session)
    romance = board.create(
        business_id=business.id, name="Romance",
        kind=BusinessUnitKind.TYPE, parent_id=tree["kdp"].id,
    )
    audience_a = board.create(
        business_id=business.id, name="AudienceA",
        kind=BusinessUnitKind.AUDIENCE, parent_id=tree["pod"].id,
    )
    skill = default_registry.skills["cooperation.ask_about"]
    with pytest.raises(SkillError, match="cross-tree query"):
        await skill.run(
            ctx=_ctx(session, business, founder, romance.id),
            args={
                "from_unit_id": str(romance.id),
                "to_unit_id": str(audience_a.id),
                "question": "x",
            },
        )
    # No CrossUnitQueryLog row should have been created (rejected at
    # gate before log_query call).
    logs = list(session.exec(select(CrossUnitQueryLog)).all())
    assert len(logs) == 0


@pytest.mark.asyncio
async def test_ask_about_skill_self_query_allowed(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """A unit asking ITSELF a question is allowed (sometimes useful
    for self-reflection prompts) and the dispatcher answers."""
    skill = default_registry.skills["cooperation.ask_about"]
    out = await skill.run(
        ctx=_ctx(session, business, founder, tree["kdp"].id),
        args={
            "from_unit_id": str(tree["kdp"].id),
            "to_unit_id": str(tree["kdp"].id),
            "question": "what am i working on?",
        },
    )
    assert out.payload["status"] == "answered"
