"""Director + Workforce tests using MockProvider."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.audit.model import InferenceTier
from korpha.blockers.model import BlockerStatus
from korpha.blockers.queue import BlockerQueue
from korpha.business.model import Business
from korpha.cofounder.director import (
    CTO_PERSONALITY,
    Director,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import RoleType
from korpha.cofounder.workforce import (
    DirectorFactory,
    DispatchSummary,
    Workforce,
)
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


def _build_director(
    session: Session,
    *,
    response: str,
    personality=CTO_PERSONALITY,
) -> Director:
    pool = InferencePool(
        providers=[MockProvider(static_response=response)], accounts=[_account()]
    )
    tracker = CostTracker(pool=pool)
    queue = BlockerQueue(session=session)
    hiring = HiringService(session)
    return Director(
        personality=personality,
        session=session,
        cost_tracker=tracker,
        queue=queue,
        hiring=hiring,
    )


def _build_workforce(session: Session, *, response: str) -> Workforce:
    pool = InferencePool(
        providers=[MockProvider(static_response=response)], accounts=[_account()]
    )
    tracker = CostTracker(pool=pool)
    queue = BlockerQueue(session=session)
    hiring = HiringService(session)
    factory = DirectorFactory(
        session=session, cost_tracker=tracker, queue=queue, hiring=hiring
    )
    return Workforce.with_default_directors(director_factory=factory)


@pytest.mark.asyncio
async def test_director_ships_when_response_says_shipped(
    session: Session, business: Business, founder: Founder
) -> None:
    response = (
        '{"status":"shipped","summary":"Deployed landing page to Vercel","detail":"Pushed to main, v1.0 live at widgetco.com"}'
    )
    director = _build_director(session, response=response)
    result = await director.attempt(
        business=business, founder=founder, task="Deploy landing page"
    )
    assert result.status == "shipped"
    assert "Deployed landing page" in result.summary
    assert result.blocker_ids == []


@pytest.mark.asyncio
async def test_director_creates_blockers_when_blocked(
    session: Session, business: Business, founder: Founder
) -> None:
    response = (
        '{"status":"blocked","summary":"Need budget choice for hosting",'
        '"blockers":['
        '{"title":"Hosting platform decision","detail":"Need to pick before deploy",'
        '"kind":"decision","urgency":"high",'
        '"options":["Vercel free","Fly.io $5/mo","Self-host VPS"]}'
        ']}'
    )
    director = _build_director(session, response=response)
    result = await director.attempt(
        business=business, founder=founder, task="Deploy landing page"
    )
    assert result.status == "blocked"
    assert len(result.blocker_ids) == 1
    queue = BlockerQueue(session=session)
    blocker = queue.get(result.blocker_ids[0])
    assert blocker.title == "Hosting platform decision"
    assert blocker.options == ["Vercel free", "Fly.io $5/mo", "Self-host VPS"]
    assert blocker.urgency.value == "high"
    assert blocker.status == BlockerStatus.OPEN


@pytest.mark.asyncio
async def test_director_falls_back_to_shipped_on_garbage_response(
    session: Session, business: Business, founder: Founder
) -> None:
    director = _build_director(session, response="this is not json at all")
    result = await director.attempt(
        business=business, founder=founder, task="Anything"
    )
    assert result.status == "shipped"
    assert result.blocker_ids == []


@pytest.mark.asyncio
async def test_director_handles_invalid_blocker_fields(
    session: Session, business: Business, founder: Founder
) -> None:
    """Missing title or unknown kind should not crash — defaults applied / item skipped."""
    response = (
        '{"status":"blocked","summary":"x",'
        '"blockers":['
        '{"title":"valid","kind":"weird-kind","urgency":"super-urgent","options":["a"]},'
        '{"title":"","kind":"info","urgency":"normal"}'
        ']}'
    )
    director = _build_director(session, response=response)
    result = await director.attempt(
        business=business, founder=founder, task="Anything"
    )
    assert len(result.blocker_ids) == 1  # empty title skipped
    queue = BlockerQueue(session=session)
    only = queue.get(result.blocker_ids[0])
    assert only.kind.value == "other"  # invalid kind → OTHER
    assert only.urgency.value == "normal"  # invalid urgency → NORMAL


def test_workforce_routes_marketing_to_cmo(session: Session) -> None:
    workforce = _build_workforce(session, response='{"status":"shipped","summary":"x"}')
    director = workforce.select_director("Draft a LinkedIn post about our launch")
    assert director.personality.role_type == RoleType.CMO


def test_workforce_routes_engineering_to_cto(session: Session) -> None:
    workforce = _build_workforce(session, response='{"status":"shipped","summary":"x"}')
    director = workforce.select_director("Deploy the new landing page")
    assert director.personality.role_type == RoleType.CTO


def test_workforce_routes_support_to_coo(session: Session) -> None:
    workforce = _build_workforce(session, response='{"status":"shipped","summary":"x"}')
    director = workforce.select_director("Set up a customer support queue")
    assert director.personality.role_type == RoleType.COO


def test_workforce_falls_back_when_no_match(session: Session) -> None:
    workforce = _build_workforce(session, response='{"status":"shipped","summary":"x"}')
    director = workforce.select_director("xyzzy plugh frobnicate")
    assert director.personality.role_type == RoleType.CTO  # default


def test_workforce_role_tag_overrides_keywords(session: Session) -> None:
    """[CMO] tag wins even when the body screams engineering."""
    workforce = _build_workforce(session, response='{"status":"shipped","summary":"x"}')
    director = workforce.select_director(
        "[CMO] Build a landing page deploy pipeline with code reviews"
    )
    assert director.personality.role_type == RoleType.CMO


def test_workforce_role_tag_case_insensitive(session: Session) -> None:
    workforce = _build_workforce(session, response='{"status":"shipped","summary":"x"}')
    director = workforce.select_director("[coo] track customer signups")
    assert director.personality.role_type == RoleType.COO


@pytest.mark.asyncio
async def test_workforce_dispatch_runs_all_tasks(
    session: Session, business: Business, founder: Founder
) -> None:
    workforce = _build_workforce(
        session,
        response='{"status":"shipped","summary":"task done","detail":"d"}',
    )
    results = await workforce.dispatch(
        business=business,
        founder=founder,
        tasks=[
            "Deploy the landing page",
            "Draft a Twitter post",
            "Set up the support queue",
        ],
    )
    assert len(results) == 3
    assert {r.role_type for r in results} == {RoleType.CTO, RoleType.CMO, RoleType.COO}
    assert all(r.status == "shipped" for r in results)


@pytest.mark.asyncio
async def test_workforce_cancel_subagent_returns_blocked_result(
    session: Session, business: Business, founder: Founder
) -> None:
    """When the founder hits /kill cto via the TUI mid-dispatch,
    the cancellation must surface as an AttemptResult with
    status='blocked' so the CEO summarizer doesn't crash. Verifies
    the dispatch wrapper catches CancelledError + returns
    _cancelled_result instead of letting the exception escape."""
    import asyncio

    from korpha.cofounder import workforce as wf

    workforce = _build_workforce(
        session,
        response='{"status":"shipped","summary":"task done","detail":"d"}',
    )

    # Patch the CTO director's attempt to take long enough that
    # we can cancel it. (MockProvider returns instantly, so without
    # patching the dispatch finishes before we can interrupt.)
    cto = workforce.directors[RoleType.CTO]
    original_attempt = cto.attempt

    async def _slow_attempt(**kw):  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)
        return await original_attempt(**kw)

    cto.attempt = _slow_attempt  # type: ignore[method-assign]

    async def _kill_after_start() -> None:
        # Wait long enough for dispatch to register the task.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if wf.list_running_subagents():
                break
        wf.cancel_subagent(str(business.id), "cto")

    killer = asyncio.create_task(_kill_after_start())
    results = await workforce.dispatch(
        business=business,
        founder=founder,
        tasks=["Deploy the landing page"],
    )
    await killer

    assert len(results) == 1
    assert results[0].status == "blocked"
    assert results[0].title == "(interrupted)"
    # Registry should be drained after dispatch returns
    assert not wf.list_running_subagents()


def test_cancel_subagent_returns_false_for_unknown_pair() -> None:
    from uuid import uuid4

    from korpha.cofounder.workforce import cancel_subagent
    assert cancel_subagent(str(uuid4()), "cto") is False


# ---- Worker tests ----


@pytest.mark.asyncio
async def test_director_spawns_copywriter_worker(
    session: Session, business: Business, founder: Founder
) -> None:
    from korpha.cofounder.director import COPYWRITER_WORKER, Worker

    director = _build_director(
        session, response='{"status":"shipped","summary":"draft"}'
    )
    worker = director.spawn_worker(business.id, "copywriter")
    assert isinstance(worker, Worker)
    assert worker.personality is COPYWRITER_WORKER
    # Worker is hired as RoleType.WORKER with specialty set.
    from sqlmodel import select

    from korpha.cofounder.hiring import HiringService
    from korpha.cofounder.model import AgentRole, RoleType

    rows = session.exec(
        select(AgentRole)
        .where(AgentRole.business_id == business.id)
        .where(AgentRole.role_type == RoleType.WORKER)
    ).all()
    assert any(r.specialty == "copywriter" and r.is_active for r in rows)
    # Reusing same specialty returns the same role.
    again = director.spawn_worker(business.id, "copywriter")
    assert again.role_id == worker.role_id
    _ = HiringService  # silence unused-import warning if reordered


def test_unknown_specialty_raises(session: Session, business: Business) -> None:
    director = _build_director(session, response='{}')
    with pytest.raises(ValueError):
        director.spawn_worker(business.id, "xyzzy")


@pytest.mark.asyncio
async def test_worker_attempt_ships(
    session: Session, business: Business, founder: Founder
) -> None:
    response = (
        '{"status":"shipped","summary":"Drafted 3 LinkedIn hooks","detail":"hook variants"}'
    )
    director = _build_director(session, response=response)
    worker = director.spawn_worker(business.id, "copywriter")
    result = await worker.attempt(
        business=business,
        founder=founder,
        task="Draft 3 LinkedIn hooks for our launch",
    )
    assert result.status == "shipped"
    assert "Drafted" in result.summary
    assert result.title == "Copywriter"


@pytest.mark.asyncio
async def test_worker_attempt_blocks(
    session: Session, business: Business, founder: Founder
) -> None:
    response = (
        '{"status":"blocked","summary":"Need brand voice direction",'
        '"blockers":[{"title":"Brand voice direction","detail":"Casual or corporate?",'
        '"kind":"clarification","urgency":"normal","options":["casual","corporate"]}]}'
    )
    director = _build_director(session, response=response)
    worker = director.spawn_worker(business.id, "copywriter")
    result = await worker.attempt(
        business=business, founder=founder, task="Draft headline copy"
    )
    assert result.status == "blocked"
    assert len(result.blocker_ids) == 1


def test_dispatch_summary_aggregates(
    session: Session, business: Business
) -> None:
    from korpha.cofounder.director import AttemptResult

    results = [
        AttemptResult(
            role_type=RoleType.CTO,
            title="CTO",
            status="shipped",
            summary="x",
            detail=None,
            blocker_ids=[],
            raw_response="",
            reasoning=None,
            cost_usd=0.001,
        ),
        AttemptResult(
            role_type=RoleType.CMO,
            title="CMO",
            status="blocked",
            summary="y",
            detail=None,
            blocker_ids=[__import__("uuid").uuid4()],
            raw_response="",
            reasoning=None,
            cost_usd=0.002,
        ),
        AttemptResult(
            role_type=RoleType.COO,
            title="COO",
            status="error",
            summary="z",
            detail="boom",
            blocker_ids=[],
            raw_response="",
            reasoning=None,
            cost_usd=0.0,
        ),
    ]
    summary = DispatchSummary.from_results(results)
    assert summary.shipped == 1
    assert summary.blocked == 1
    assert summary.errored == 1
    assert summary.total_blockers == 1
    assert "1 shipped" in summary.headline()
