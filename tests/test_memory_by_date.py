"""Tests for memory.recall_by_date — date parsing + SQL roundtrip."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.model import (
    AgentRole, Message, MessageSenderType, RoleType,
    Thread, ThreadPlatform, ThreadStatus,
)
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.skills.memory_by_date import MemoryRecallByDateSkill
from korpha.skills.types import SkillContext, SkillError


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


def _seed_business(engine: Engine) -> tuple[Founder, Business]:
    fid = uuid4()
    bid = uuid4()
    email = f"{fid}@x.com"
    with Session(engine) as s:
        f = Founder(id=fid, email=email)
        b = Business(id=bid, founder_id=fid, name="Test")
        s.add(f); s.add(b)
        s.commit()
        s.refresh(f); s.refresh(b)
        return Founder(id=fid, email=email), Business(id=bid, founder_id=fid, name="Test")


def _seed_thread(
    engine: Engine, *, business_id: UUID, founder_id: UUID,
    topic: str = "test thread", platform: ThreadPlatform = ThreadPlatform.WEB,
    when: datetime,
) -> UUID:
    tid = uuid4()
    rid = uuid4()
    with Session(engine) as s:
        s.add(AgentRole(
            id=rid, business_id=business_id,
            role_type=RoleType.CEO, title="CEO",
        ))
        s.add(Thread(
            id=tid, business_id=business_id, founder_id=founder_id,
            agent_role_id=rid, platform=platform,
            topic=topic, status=ThreadStatus.ACTIVE,
            created_at=when, last_message_at=when,
        ))
        s.commit()
    return tid


def _seed_message(
    engine: Engine, *, thread_id: UUID, content: str, when: datetime,
    role: MessageSenderType = MessageSenderType.FOUNDER,
) -> None:
    with Session(engine) as s:
        s.add(Message(
            id=uuid4(), thread_id=thread_id,
            sender_type=role, content=content,
            created_at=when,
        ))
        s.commit()


def _ctx(engine: Engine, business: Business, founder: Founder) -> SkillContext:
    return SkillContext(
        business=business,
        founder=founder,
        session=Session(engine),
        cost_tracker=CostTracker(pool=None),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Empty / error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_when_arg_raises(engine: Engine) -> None:
    f, b = _seed_business(engine)
    skill = MemoryRecallByDateSkill()
    with pytest.raises(SkillError, match="'when' is required"):
        await skill.run(ctx=_ctx(engine, b, f), args={})


@pytest.mark.asyncio
async def test_unparseable_when_raises(engine: Engine) -> None:
    f, b = _seed_business(engine)
    skill = MemoryRecallByDateSkill()
    with pytest.raises(SkillError, match="couldn't parse"):
        await skill.run(ctx=_ctx(engine, b, f), args={"when": "foo bar"})


@pytest.mark.asyncio
async def test_empty_window_returns_no_messages(engine: Engine) -> None:
    f, b = _seed_business(engine)
    skill = MemoryRecallByDateSkill()
    result = await skill.run(
        ctx=_ctx(engine, b, f), args={"when": "May 10 2025"},
    )
    assert "No conversations" in result.summary
    assert result.payload["message_count"] == 0
    assert result.payload["thread_count"] == 0


# ---------------------------------------------------------------------------
# Happy path — seed messages + query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_messages_in_range(engine: Engine) -> None:
    f, b = _seed_business(engine)
    when_in = datetime(2026, 5, 10, 14, 30, tzinfo=UTC)
    when_out = datetime(2026, 5, 12, 14, 30, tzinfo=UTC)
    tid = _seed_thread(
        engine, business_id=b.id, founder_id=f.id,
        topic="niche research", when=when_in,
    )
    _seed_message(engine, thread_id=tid, content="What's the niche?", when=when_in)
    _seed_message(engine, thread_id=tid, content="Generate 5 ideas", when=when_in)
    _seed_message(engine, thread_id=tid, content="Out-of-window msg", when=when_out)

    skill = MemoryRecallByDateSkill()
    result = await skill.run(
        ctx=_ctx(engine, b, f), args={"when": "2026-05-10"},
    )
    # Only 2 messages should show (the third is outside the range)
    assert result.payload["message_count"] == 2
    assert result.payload["thread_count"] == 1
    assert result.payload["threads"][0]["topic"] == "niche research"
    assert "What's the niche?" in result.summary
    assert "Out-of-window msg" not in result.summary


@pytest.mark.asyncio
async def test_recall_max_messages_cap(engine: Engine) -> None:
    """Hard cap on returned messages — payload trims but counts stay
    accurate."""
    f, b = _seed_business(engine)
    when = datetime(2026, 5, 10, 14, 30, tzinfo=UTC)
    tid = _seed_thread(
        engine, business_id=b.id, founder_id=f.id, when=when,
    )
    for i in range(20):
        _seed_message(
            engine, thread_id=tid,
            content=f"msg {i}",
            when=datetime(2026, 5, 10, 14, 30 + i, tzinfo=UTC),
        )

    skill = MemoryRecallByDateSkill()
    result = await skill.run(
        ctx=_ctx(engine, b, f),
        args={"when": "2026-05-10", "max_messages": 5},
    )
    assert result.payload["message_count"] == 20  # actual count
    assert result.payload["shown_count"] == 5     # capped
    assert len(result.payload["messages"]) == 5
    assert "Showing first 5" in result.summary


@pytest.mark.asyncio
async def test_recall_platform_filter(engine: Engine) -> None:
    f, b = _seed_business(engine)
    when = datetime(2026, 5, 10, 14, 30, tzinfo=UTC)
    web_tid = _seed_thread(
        engine, business_id=b.id, founder_id=f.id,
        topic="web chat", platform=ThreadPlatform.WEB, when=when,
    )
    email_tid = _seed_thread(
        engine, business_id=b.id, founder_id=f.id,
        topic="email exchange", platform=ThreadPlatform.EMAIL, when=when,
    )
    _seed_message(engine, thread_id=web_tid, content="web msg", when=when)
    _seed_message(engine, thread_id=email_tid, content="email msg", when=when)

    skill = MemoryRecallByDateSkill()
    web_only = await skill.run(
        ctx=_ctx(engine, b, f),
        args={"when": "2026-05-10", "platform": "web"},
    )
    assert web_only.payload["thread_count"] == 1
    assert web_only.payload["threads"][0]["platform"] == "web"
    assert "web msg" in web_only.summary
    assert "email msg" not in web_only.summary


@pytest.mark.asyncio
async def test_recall_unknown_platform_raises(engine: Engine) -> None:
    f, b = _seed_business(engine)
    skill = MemoryRecallByDateSkill()
    with pytest.raises(SkillError, match="unknown platform"):
        await skill.run(
            ctx=_ctx(engine, b, f),
            args={"when": "today", "platform": "nonsense"},
        )


@pytest.mark.asyncio
async def test_recall_excludes_other_business(engine: Engine) -> None:
    """Messages from another business must NOT show up — tenant isolation."""
    f, b = _seed_business(engine)
    f2, b2 = _seed_business(engine)
    when = datetime(2026, 5, 10, tzinfo=UTC)
    mine = _seed_thread(engine, business_id=b.id, founder_id=f.id, when=when)
    theirs = _seed_thread(engine, business_id=b2.id, founder_id=f2.id, when=when)
    _seed_message(engine, thread_id=mine, content="my message", when=when)
    _seed_message(engine, thread_id=theirs, content="THEIR message", when=when)

    skill = MemoryRecallByDateSkill()
    result = await skill.run(
        ctx=_ctx(engine, b, f), args={"when": "2026-05-10"},
    )
    assert result.payload["message_count"] == 1
    assert "my message" in result.summary
    assert "THEIR message" not in result.summary


# ---------------------------------------------------------------------------
# Skill registration sanity
# ---------------------------------------------------------------------------


def test_skill_is_in_default_registry() -> None:
    """Auto-loaded via korpha/skills/__init__.py."""
    from korpha.skills import default_registry
    names = {s.name for s in default_registry.list_specs()}
    assert "memory.recall_by_date" in names
