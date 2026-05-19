"""Tests for the /goal Ralph loop."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from korpha.business.model import Business
from korpha.cofounder.model import (
    Thread, ThreadPlatform, ThreadStatus,
)
from korpha.goals import (
    DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES,
    Goal,
    GoalManager,
    GoalStatus,
    JudgeVerdict,
    parse_judge_response,
)
from korpha.goals.judge import truncate_response
from korpha.identity.model import Founder


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path}/goals.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session) -> tuple[UUID, UUID]:
    """Returns (business_id, thread_id)."""
    from korpha.cofounder.model import AgentRole, RoleType
    f = Founder(email="x@y.com", display_name="Mike")
    session.add(f); session.commit(); session.refresh(f)
    b = Business(
        founder_id=f.id, name="WidgetCo", description="t",
    )
    session.add(b); session.commit(); session.refresh(b)
    role = AgentRole(
        business_id=b.id, role_type=RoleType.CEO, title="CEO",
    )
    session.add(role); session.commit(); session.refresh(role)
    t = Thread(
        business_id=b.id, founder_id=f.id,
        agent_role_id=role.id, platform=ThreadPlatform.WEB,
        status=ThreadStatus.ACTIVE, topic="t",
    )
    session.add(t); session.commit(); session.refresh(t)
    return b.id, t.id


# ---- parse_judge_response ----


@pytest.mark.parametrize("raw,expected_done,expected_parsed", [
    ('{"done": true, "reason": "all set"}', True, True),
    ('{"done": false, "reason": "not yet"}', False, True),
    # Tolerant parsing: markdown fence, leading prose
    ('Here is my verdict:\n```json\n{"done": true, "reason": "ok"}\n```', True, True),
    ('Sure!\n{"done": false, "reason": "needs more"}\nThanks.', False, True),
    # String-typed bool (some small models stringify)
    ('{"done": "true", "reason": "yes"}', True, True),
    ('{"done": "false", "reason": "no"}', False, True),
])
def test_parse_judge_response_accepts_known_shapes(
    raw: str, expected_done: bool, expected_parsed: bool,
) -> None:
    out = parse_judge_response(raw)
    assert out.done == expected_done
    assert out.parsed == expected_parsed


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "I think it's done",  # no JSON
    "{not json}",
    '{"reason": "missing done key"}',
])
def test_parse_judge_response_marks_unparseable(raw: str) -> None:
    out = parse_judge_response(raw)
    assert out.parsed is False


def test_parse_judge_response_string_bool_treated_lenient() -> None:
    """Models that stringify the bool — we accept and resolve to
    False for anything that's not in the truthy set."""
    out = parse_judge_response('{"done": "maybe", "reason": "unclear"}')
    assert out.parsed is True
    assert out.done is False  # 'maybe' falls through to False


def test_truncate_response_passes_through_short() -> None:
    assert truncate_response("hi", limit=100) == "hi"


def test_truncate_response_keeps_head_and_tail() -> None:
    text = "X" * 500
    out = truncate_response(text, limit=100)
    assert "[truncated]" in out
    assert len(out) <= 200  # head 100-200 + tail 150 + marker is well under


# ---- GoalManager: set / pause / resume / clear ----


def _stub_cost_tracker(verdict_text: str = ""):
    """Returns a CostTracker stub that returns ``verdict_text`` from
    every complete() call."""
    class _Stub:
        async def complete(self, request, **_kw):
            class _Resp:
                content = verdict_text
                cost_usd = 0.0
            return _Resp()
    return _Stub()


def test_set_creates_active_goal(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    goal = mgr.set("get me 10 customers")
    assert goal.text == "get me 10 customers"
    assert goal.status == GoalStatus.ACTIVE
    assert goal.turns_used == 0
    # active() finds it
    assert mgr.active() is not None
    assert mgr.active().id == goal.id


def test_set_replaces_existing_active_goal(session: Session) -> None:
    """Replacement requires force=True since the mid-run guard
    landed (Hermes /goal parity). Replace mechanics unchanged."""
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    first = mgr.set("first")
    second = mgr.set("second", force=True)
    # First moved to CLEARED, second is active
    session.refresh(first)
    assert first.status == GoalStatus.CLEARED
    assert first.paused_reason == "replaced-by-new-goal"
    assert second.status == GoalStatus.ACTIVE
    # Only one active for this thread
    actives = list(session.exec(
        select(Goal).where(Goal.thread_id == thread).where(
            Goal.status == GoalStatus.ACTIVE,
        ),
    ).all())
    assert len(actives) == 1


def test_set_rejects_empty_text(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    with pytest.raises(ValueError, match="empty"):
        mgr.set("   ")
    with pytest.raises(ValueError, match="empty"):
        mgr.set("")


def test_set_rejects_zero_max_turns(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    with pytest.raises(ValueError, match="max_turns"):
        mgr.set("x", max_turns=0)


def test_pause_moves_active_to_paused(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    mgr.set("x")
    paused = mgr.pause()
    assert paused.status == GoalStatus.PAUSED
    assert paused.paused_reason == "user-paused"
    assert mgr.active() is None


def test_pause_returns_none_when_no_active(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    assert mgr.pause() is None


def test_resume_re_activates_paused_goal(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    mgr.set("x", max_turns=10)
    g = mgr.active()
    g.turns_used = 7
    g.consecutive_parse_failures = 2
    session.add(g); session.commit()
    mgr.pause()
    # Resume with budget reset (default)
    resumed = mgr.resume()
    assert resumed.status == GoalStatus.ACTIVE
    assert resumed.turns_used == 0
    assert resumed.consecutive_parse_failures == 0
    assert resumed.paused_reason is None


def test_resume_can_preserve_budget(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    mgr.set("x", max_turns=10)
    g = mgr.active()
    g.turns_used = 7
    session.add(g); session.commit()
    mgr.pause()
    resumed = mgr.resume(reset_budget=False)
    assert resumed.turns_used == 7  # preserved


def test_clear_drops_active_goal(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    mgr.set("x")
    cleared = mgr.clear()
    assert cleared.status == GoalStatus.CLEARED
    assert cleared.finished_at is not None
    assert mgr.active() is None


def test_mark_done_force_completes(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    mgr.set("x")
    done = mgr.mark_done("founder said so")
    assert done.status == GoalStatus.DONE
    assert done.last_reason == "founder said so"


# ---- evaluate_after_turn ----


@pytest.mark.asyncio
async def test_judge_done_marks_goal_done(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(
            '{"done": true, "reason": "shipped"}'
        ),
    )
    mgr.set("ship the thing")
    after = await mgr.evaluate_after_turn(
        last_response="OK, shipped it. Diff merged.",
    )
    assert after.status == GoalStatus.DONE
    assert after.last_verdict == "done"
    assert after.last_reason == "shipped"
    assert after.turns_used == 1


@pytest.mark.asyncio
async def test_judge_continue_keeps_goal_active(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(
            '{"done": false, "reason": "more work needed"}'
        ),
    )
    mgr.set("ship the thing", max_turns=5)
    after = await mgr.evaluate_after_turn(
        last_response="working on it...",
    )
    assert after.status == GoalStatus.ACTIVE
    assert after.last_verdict == "continue"
    assert after.turns_used == 1


@pytest.mark.asyncio
async def test_turn_budget_pauses_loop(session: Session) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(
            '{"done": false, "reason": "nope"}'
        ),
    )
    mgr.set("never done", max_turns=2)
    await mgr.evaluate_after_turn(last_response="t1")
    assert mgr.active() is not None  # still active after 1
    await mgr.evaluate_after_turn(last_response="t2")
    paused = mgr.latest()
    assert paused.status == GoalStatus.PAUSED
    assert paused.paused_reason == "turn-budget"


@pytest.mark.asyncio
async def test_three_parse_failures_pause_loop(
    session: Session,
) -> None:
    """The judge can't follow JSON contract → after N failures
    auto-pause + tell the founder."""
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker("garbage non-json"),
    )
    mgr.set("x", max_turns=99)
    for _ in range(DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES):
        await mgr.evaluate_after_turn(last_response="...")
    paused = mgr.latest()
    assert paused.status == GoalStatus.PAUSED
    assert paused.paused_reason == "judge-parse-failures"


@pytest.mark.asyncio
async def test_judge_transport_failure_treated_as_continue(
    session: Session,
) -> None:
    """Network blip → fail-OPEN. Doesn't count toward parse-
    failure budget. Loop keeps going; turn-budget is the backstop."""
    biz, thread = _seed(session)

    class _BoomTracker:
        async def complete(self, *_a, **_k):
            raise RuntimeError("network down")

    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_BoomTracker(),
    )
    mgr.set("x", max_turns=99)
    after = await mgr.evaluate_after_turn(last_response="...")
    assert after.status == GoalStatus.ACTIVE  # didn't pause
    assert after.consecutive_parse_failures == 0  # didn't count
    assert "judge call failed" in after.last_reason


@pytest.mark.asyncio
async def test_parse_failure_counter_resets_on_success(
    session: Session,
) -> None:
    """A failure followed by a clean reply resets the counter,
    so we don't cumulatively pause from intermittent flakes."""
    biz, thread = _seed(session)

    responses = iter([
        "garbage",
        "still garbage",
        '{"done": false, "reason": "ok now"}',
        "garbage again",
    ])

    class _Tracker:
        async def complete(self, *_a, **_k):
            class _R:
                content = next(responses)
                cost_usd = 0.0
            return _R()

    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_Tracker(),
    )
    mgr.set("x", max_turns=99)
    await mgr.evaluate_after_turn(last_response="t1")
    await mgr.evaluate_after_turn(last_response="t2")
    g = mgr.active()
    assert g.consecutive_parse_failures == 2
    # Third turn parses cleanly
    await mgr.evaluate_after_turn(last_response="t3")
    g = mgr.active()
    assert g.consecutive_parse_failures == 0
    # Fourth turn fails again — counter starts from 0, no auto-pause
    await mgr.evaluate_after_turn(last_response="t4")
    g = mgr.active()
    assert g.status == GoalStatus.ACTIVE
    assert g.consecutive_parse_failures == 1


# ---- continuation prompt ----


def test_next_continuation_prompt_includes_goal(
    session: Session,
) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    mgr.set("get me 10 customers")
    prompt = mgr.next_continuation_prompt()
    assert prompt is not None
    assert "get me 10 customers" in prompt
    assert "Continuing toward" in prompt


def test_next_continuation_prompt_returns_none_when_no_goal(
    session: Session,
) -> None:
    biz, thread = _seed(session)
    mgr = GoalManager(
        session=session, thread_id=thread, business_id=biz,
        cost_tracker=_stub_cost_tracker(),
    )
    assert mgr.next_continuation_prompt() is None
