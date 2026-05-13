"""Tests for the continuation-summary builder."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from korpha.continuation import (
    ContinuationSummary,
    summarize_attempts,
    summarize_goal_history,
)


# ---- duck-typed AttemptResult-shaped fakes ----


@dataclass(frozen=True)
class _FakeRole:
    value: str


@dataclass(frozen=True)
class _FakeAttempt:
    role_type: _FakeRole
    status: str
    summary: str
    detail: str = ""
    blocker_ids: tuple[UUID, ...] = ()


def _att(role: str, status: str, summary: str, **kw) -> _FakeAttempt:
    return _FakeAttempt(
        role_type=_FakeRole(value=role),
        status=status, summary=summary, **kw,
    )


# ---- summarize_attempts ----


def test_summarize_empty_returns_just_header() -> None:
    summary = summarize_attempts([])
    assert summary.char_count > 0
    assert summary.paths == ()
    assert summary.truncated is False


def test_summarize_one_attempt() -> None:
    summary = summarize_attempts([
        _att("cto", "shipped", "deployed at https://example.com/a"),
    ])
    assert "[CTO] shipped" in summary.text
    assert "https://example.com/a" in summary.text
    assert "https://example.com/a" in summary.paths


def test_summarize_extracts_file_paths() -> None:
    summary = summarize_attempts([
        _att(
            "cto", "shipped",
            "Wrote korpha/cofounder/ceo.py and tests/test_ceo.py",
        ),
    ])
    assert "korpha/cofounder/ceo.py" in summary.paths
    assert "tests/test_ceo.py" in summary.paths


def test_summarize_dedupes_paths() -> None:
    summary = summarize_attempts([
        _att(
            "cto", "shipped",
            "Wrote korpha/foo.py once and korpha/foo.py again",
        ),
    ])
    assert summary.paths.count("korpha/foo.py") == 1


def test_summarize_collects_blocker_ids() -> None:
    bid = uuid4()
    summary = summarize_attempts([
        _att("cmo", "blocked", "needs decision", blocker_ids=(bid,)),
    ])
    assert bid in summary.blocker_ids


def test_summarize_truncates_oldest_when_over_budget() -> None:
    """Many entries → drop from front, mark truncated."""
    attempts = [
        _att("cto", "shipped", f"task-number-{i}-ipsum-lorem")
        for i in range(40)
    ]
    summary = summarize_attempts(attempts, char_budget=200)
    assert summary.truncated is True
    assert summary.char_count <= 250  # budget + small slop
    assert "[older entries truncated]" in summary.text


def test_summarize_preserves_recent_when_truncating() -> None:
    """The newest entries should always survive."""
    attempts = [
        _att("cto", "shipped", f"old-task-{i}")
        for i in range(20)
    ] + [
        _att("cto", "shipped", "FRESH-MARKER"),
    ]
    summary = summarize_attempts(attempts, char_budget=200)
    assert "FRESH-MARKER" in summary.text


def test_summarize_chronological_order() -> None:
    summary = summarize_attempts([
        _att("cto", "shipped", "first"),
        _att("cmo", "shipped", "second"),
    ])
    first_pos = summary.text.find("first")
    second_pos = summary.text.find("second")
    assert 0 < first_pos < second_pos


def test_path_extractor_skips_bare_words() -> None:
    summary = summarize_attempts([
        _att("cto", "shipped", "Something something with no real path"),
    ])
    assert summary.paths == ()


# ---- summarize_goal_history ----


def test_goal_history_returns_none_when_no_goal(
    session,
) -> None:
    assert summarize_goal_history(session, uuid4()) is None


def test_goal_history_returns_none_when_no_turns_yet(
    session, business, founder,
) -> None:
    """Continuation only matters after at least one turn."""
    from korpha.cofounder.model import (
        AgentRole, Thread, ThreadPlatform, RoleType,
    )
    from korpha.goals.model import Goal

    role = AgentRole(
        business_id=business.id, role_type=RoleType.CEO, title="CEO",
    )
    session.add(role); session.commit(); session.refresh(role)
    thread = Thread(
        business_id=business.id, founder_id=founder.id,
        agent_role_id=role.id, platform=ThreadPlatform.WEB,
    )
    session.add(thread); session.commit(); session.refresh(thread)
    goal = Goal(
        thread_id=thread.id, business_id=business.id,
        text="ship the demo", turns_used=0,
    )
    session.add(goal); session.commit(); session.refresh(goal)
    assert summarize_goal_history(session, goal.id) is None


def test_goal_history_includes_verdict_and_paused_reason(
    session, business, founder,
) -> None:
    from korpha.cofounder.model import (
        AgentRole, Thread, ThreadPlatform, RoleType,
    )
    from korpha.goals.model import Goal

    role = AgentRole(
        business_id=business.id, role_type=RoleType.CEO, title="CEO",
    )
    session.add(role); session.commit(); session.refresh(role)
    thread = Thread(
        business_id=business.id, founder_id=founder.id,
        agent_role_id=role.id, platform=ThreadPlatform.WEB,
    )
    session.add(thread); session.commit(); session.refresh(thread)
    goal = Goal(
        thread_id=thread.id, business_id=business.id,
        text="get 10 paying customers",
        turns_used=3, max_turns=20,
        last_verdict="continue",
        last_reason="2 of 10 booked, keep going",
        paused_reason=None,
    )
    session.add(goal); session.commit(); session.refresh(goal)
    summary = summarize_goal_history(session, goal.id)
    assert summary is not None
    assert "get 10 paying customers" in summary.text
    assert "Turns used: 3/20" in summary.text
    assert "continue" in summary.text
    assert "2 of 10 booked" in summary.text


def test_goal_history_truncates_long_goal_text(
    session, business, founder,
) -> None:
    from korpha.cofounder.model import (
        AgentRole, Thread, ThreadPlatform, RoleType,
    )
    from korpha.goals.model import Goal

    role = AgentRole(
        business_id=business.id, role_type=RoleType.CEO, title="CEO",
    )
    session.add(role); session.commit(); session.refresh(role)
    thread = Thread(
        business_id=business.id, founder_id=founder.id,
        agent_role_id=role.id, platform=ThreadPlatform.WEB,
    )
    session.add(thread); session.commit(); session.refresh(thread)
    goal = Goal(
        thread_id=thread.id, business_id=business.id,
        text="x" * 5000,
        turns_used=1,
    )
    session.add(goal); session.commit(); session.refresh(goal)
    summary = summarize_goal_history(
        session, goal.id, char_budget=300,
    )
    assert summary is not None
    assert summary.char_count <= 350


# ---- GoalManager continuation prefix ----


def test_goal_manager_prepends_continuation_after_turn(
    session, business, founder,
) -> None:
    """After turn 1, next_continuation_prompt() includes the
    bounded continuation block."""
    from korpha.cofounder.model import (
        AgentRole, Thread, ThreadPlatform, RoleType,
    )
    from korpha.goals.manager import GoalManager
    from korpha.goals.model import Goal
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    role = AgentRole(
        business_id=business.id, role_type=RoleType.CEO, title="CEO",
    )
    session.add(role); session.commit(); session.refresh(role)
    thread = Thread(
        business_id=business.id, founder_id=founder.id,
        agent_role_id=role.id, platform=ThreadPlatform.WEB,
    )
    session.add(thread); session.commit(); session.refresh(thread)
    goal = Goal(
        thread_id=thread.id, business_id=business.id,
        text="ship the demo",
        turns_used=2, last_verdict="continue",
        last_reason="halfway there",
    )
    session.add(goal); session.commit(); session.refresh(goal)

    pool = InferencePool(providers=[], accounts=[])
    mgr = GoalManager(
        session=session, thread_id=thread.id,
        cost_tracker=CostTracker(pool=pool),
        business_id=business.id,
    )
    prompt = mgr.next_continuation_prompt()
    assert prompt is not None
    assert "Continuation context" in prompt
    assert "halfway there" in prompt
    assert "ship the demo" in prompt
