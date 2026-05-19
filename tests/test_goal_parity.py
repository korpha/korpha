"""Tests for the Hermes /goal parity gaps: shared slash parser,
mid-run safety guard on GoalManager.set, bare-status alias.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.model import (
    AgentRole, RoleType, Thread, ThreadPlatform, ThreadStatus,
)
from korpha.goals import (
    GoalManager,
    GoalReplaceConflict,
    execute_goal_slash,
    is_goal_slash,
    parse_goal_slash,
)
from korpha.goals.model import GoalStatus
from korpha.identity.model import Founder


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_is_goal_slash_accepts_real_forms() -> None:
    assert is_goal_slash("/goal")
    assert is_goal_slash("/goal status")
    assert is_goal_slash("/goal fix lint errors")
    assert is_goal_slash("  /goal pause  ")


def test_is_goal_slash_rejects_lookalikes() -> None:
    assert not is_goal_slash("/goalkeeper")
    assert not is_goal_slash("/goals")
    assert not is_goal_slash("goal status")
    assert not is_goal_slash("")
    assert not is_goal_slash("hello /goal")


def test_bare_goal_parses_as_status() -> None:
    intent = parse_goal_slash("/goal")
    assert intent.action == "status"


def test_goal_status_parses() -> None:
    intent = parse_goal_slash("/goal status")
    assert intent.action == "status"


def test_set_parses_with_text() -> None:
    intent = parse_goal_slash("/goal fix every failing test")
    assert intent.action == "set"
    assert intent.text == "fix every failing test"
    assert intent.force is False


def test_set_force_parses() -> None:
    intent = parse_goal_slash("/goal --force replace it now")
    assert intent.action == "set"
    assert intent.text == "replace it now"
    assert intent.force is True


def test_pause_resume_clear_help_parse() -> None:
    assert parse_goal_slash("/goal pause").action == "pause"
    assert parse_goal_slash("/goal resume").action == "resume"
    assert parse_goal_slash("/goal clear").action == "clear"
    assert parse_goal_slash("/goal help").action == "help"


def test_subcommand_case_insensitive() -> None:
    assert parse_goal_slash("/goal STATUS").action == "status"
    assert parse_goal_slash("/goal Pause").action == "pause"


def test_non_goal_input_returns_unknown() -> None:
    intent = parse_goal_slash("hello world")
    assert intent.action == "unknown"


# ---------------------------------------------------------------------------
# Mid-run safety guard
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    from sqlalchemy import StaticPool

    e = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(e)
    return e


@pytest.fixture()
def thread_id(engine: Engine) -> tuple[UUID, UUID]:
    """Seeded business + active web thread → (business_id, thread_id)."""
    bid = uuid4()
    tid = uuid4()
    fid = uuid4()
    rid = uuid4()
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(Founder(id=fid, email="m@x.com"))
        s.add(Business(id=bid, founder_id=fid, name="Test"))
        s.add(AgentRole(
            id=rid, business_id=bid,
            role_type=RoleType.CEO, title="CEO",
        ))
        s.add(Thread(
            id=tid, business_id=bid, founder_id=fid,
            agent_role_id=rid,
            platform=ThreadPlatform.WEB,
            status=ThreadStatus.ACTIVE,
            last_message_at=now,
        ))
        s.commit()
    return bid, tid


def _manager(engine: Engine, business_id: UUID, thread_id: UUID) -> GoalManager:
    return GoalManager(
        session=Session(engine), thread_id=thread_id,
        business_id=business_id, cost_tracker=None,
    )


def test_set_with_no_active_goal_succeeds(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        g = mgr.set("ship the landing page")
        assert g.text == "ship the landing page"


def test_set_with_active_goal_refuses_without_force(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        mgr.set("original goal")
        with pytest.raises(GoalReplaceConflict, match="active goal exists"):
            mgr.set("replacement attempt")


def test_set_with_active_goal_force_replaces(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        mgr.set("original goal")
        g = mgr.set("replacement", force=True)
        assert g.text == "replacement"
        assert mgr.active().text == "replacement"


def test_set_after_pause_works_without_force(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    """Paused goal isn't ACTIVE, so the guard shouldn't trip — pause
    means the loop isn't running, no race possible."""
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        mgr.set("first goal")
        mgr.pause()
        # No force needed
        g = mgr.set("second goal")
        assert g.text == "second goal"


def test_set_after_clear_works_without_force(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        mgr.set("first goal")
        mgr.clear()
        g = mgr.set("second goal")
        assert g.text == "second goal"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def test_executor_status_when_no_goal(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        reply = execute_goal_slash(parse_goal_slash("/goal"), mgr)
    assert "no goal set" in reply.lower()


def test_executor_set_then_status(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        r1 = execute_goal_slash(
            parse_goal_slash("/goal fix lint errors"), mgr,
        )
        assert "Goal set" in r1
        assert "fix lint errors" in r1
        r2 = execute_goal_slash(parse_goal_slash("/goal"), mgr)
        assert "Active goal" in r2
        assert "fix lint errors" in r2


def test_executor_set_refuses_when_active(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        execute_goal_slash(parse_goal_slash("/goal first"), mgr)
        reply = execute_goal_slash(parse_goal_slash("/goal second"), mgr)
    assert "active goal exists" in reply.lower()
    assert "--force" in reply or "force" in reply


def test_executor_set_force_replaces(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        execute_goal_slash(parse_goal_slash("/goal first"), mgr)
        reply = execute_goal_slash(
            parse_goal_slash("/goal --force second"), mgr,
        )
    assert "Goal set" in reply
    assert "second" in reply


def test_executor_pause_resume_clear_flow(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        execute_goal_slash(parse_goal_slash("/goal x"), mgr)
        assert "Paused" in execute_goal_slash(
            parse_goal_slash("/goal pause"), mgr,
        )
        assert "Resumed" in execute_goal_slash(
            parse_goal_slash("/goal resume"), mgr,
        )
        assert "Cleared" in execute_goal_slash(
            parse_goal_slash("/goal clear"), mgr,
        )


def test_executor_help_returns_usage(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        reply = execute_goal_slash(parse_goal_slash("/goal help"), mgr)
    assert "/goal <text>" in reply
    assert "--force" in reply


def test_executor_empty_set_rejects(
    engine: Engine, thread_id: tuple[UUID, UUID],
) -> None:
    bid, tid = thread_id
    with Session(engine) as s:
        mgr = GoalManager(
            session=s, thread_id=tid, business_id=bid, cost_tracker=None,
        )
        # parse_goal_slash on "/goal " gives status (no text), so test
        # the set-with-empty-text path directly via the executor
        from korpha.goals import GoalSlashIntent
        reply = execute_goal_slash(
            GoalSlashIntent(action="set", text="", raw="/goal --force"), mgr,
        )
    assert "required" in reply.lower()
