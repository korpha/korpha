"""Tests for the DB-backed default LongTermMemory implementation
+ the memory.remember / memory.recall skills."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.memory import MemoryQuery, MemoryEntry
from korpha.memory.db_backend import DbLongTermMemory
from korpha.memory.model import LongTermMemoryEntry  # noqa: F401


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path}/mem.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session) -> tuple[UUID, UUID]:
    f = Founder(email="x@y.com", display_name="Mike")
    session.add(f); session.commit(); session.refresh(f)
    b = Business(
        founder_id=f.id, name="WidgetCo", description="t",
    )
    session.add(b); session.commit(); session.refresh(b)
    return b.id, f.id


# ---- DbLongTermMemory: add ----


@pytest.mark.asyncio
async def test_add_persists_entry(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    entry = await mem.add(
        business_id=biz, founder_id=founder,
        text="Targeting freelance designers",
        tags=["niche"],
    )
    assert entry.text == "Targeting freelance designers"
    assert entry.tags == ("niche",)
    # Confirm row in DB
    row = session.get(LongTermMemoryEntry, UUID(entry.id))
    assert row is not None
    assert row.business_id == biz


@pytest.mark.asyncio
async def test_add_rejects_empty_text(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    with pytest.raises(ValueError, match="empty"):
        await mem.add(
            business_id=biz, founder_id=founder, text="   ",
        )


# ---- DbLongTermMemory: search ----


@pytest.mark.asyncio
async def test_search_returns_matching_entries(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    await mem.add(
        business_id=biz, founder_id=founder,
        text="Targeting freelance designers in NYC",
    )
    await mem.add(
        business_id=biz, founder_id=founder,
        text="Stripe configured for $29/month",
    )
    hits = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder, text="freelance",
    ))
    assert len(hits) == 1
    assert "designers" in hits[0].text


@pytest.mark.asyncio
async def test_search_isolates_by_business(session: Session) -> None:
    """Multi-tenant: business A's memories don't leak into B's
    search results, even with the same founder."""
    biz_a, founder = _seed(session)
    biz_b = Business(
        founder_id=founder, name="OtherCo", description="",
    )
    session.add(biz_b); session.commit(); session.refresh(biz_b)

    mem = DbLongTermMemory(session)
    await mem.add(
        business_id=biz_a, founder_id=founder,
        text="biz A secret targeting freelance",
    )
    await mem.add(
        business_id=biz_b.id, founder_id=founder,
        text="biz B secret targeting freelance",
    )
    hits = await mem.search(MemoryQuery(
        business_id=biz_a, founder_id=founder, text="freelance",
    ))
    assert len(hits) == 1
    assert "A secret" in hits[0].text


@pytest.mark.asyncio
async def test_search_filters_by_tags(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    await mem.add(
        business_id=biz, founder_id=founder,
        text="targeting freelance designers",
        tags=["niche"],
    )
    await mem.add(
        business_id=biz, founder_id=founder,
        text="stripe key set up freelance trial",
        tags=["billing"],
    )
    # Only return niche-tagged entries even though both match the query
    hits = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder,
        text="freelance", tags=("niche",),
    ))
    assert len(hits) == 1
    assert "niche" in hits[0].tags


@pytest.mark.asyncio
async def test_search_returns_empty_for_no_match(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    await mem.add(
        business_id=biz, founder_id=founder,
        text="targeting freelance designers",
    )
    hits = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder,
        text="cryptocurrency mining rig setup",
    ))
    assert hits == []


@pytest.mark.asyncio
async def test_search_returns_empty_for_blank_query(
    session: Session,
) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    await mem.add(
        business_id=biz, founder_id=founder, text="anything",
    )
    hits = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder, text="   ",
    ))
    assert hits == []


@pytest.mark.asyncio
async def test_search_respects_limit(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    for i in range(10):
        await mem.add(
            business_id=biz, founder_id=founder,
            text=f"freelance designer note {i}",
        )
    hits = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder,
        text="freelance", limit=3,
    ))
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_search_skill_score_populated(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    await mem.add(
        business_id=biz, founder_id=founder,
        text="freelance designers stripe setup",
    )
    hits = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder,
        text="freelance stripe",
    ))
    assert hits[0].score == pytest.approx(1.0)  # both tokens matched


# ---- DbLongTermMemory: forget ----


@pytest.mark.asyncio
async def test_forget_removes_entry(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    entry = await mem.add(
        business_id=biz, founder_id=founder, text="x",
    )
    ok = await mem.forget(
        business_id=biz, founder_id=founder, memory_id=entry.id,
    )
    assert ok is True
    assert session.get(LongTermMemoryEntry, UUID(entry.id)) is None


@pytest.mark.asyncio
async def test_forget_unknown_id_returns_false(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    ok = await mem.forget(
        business_id=biz, founder_id=founder,
        memory_id=str(uuid4()),
    )
    assert ok is False


@pytest.mark.asyncio
async def test_forget_refuses_cross_tenant(session: Session) -> None:
    """Multi-tenant: founder A can't forget founder B's memory
    even if they know the id."""
    biz_a, founder = _seed(session)
    biz_b = Business(
        founder_id=founder, name="Other", description="",
    )
    session.add(biz_b); session.commit(); session.refresh(biz_b)
    mem = DbLongTermMemory(session)
    entry = await mem.add(
        business_id=biz_b.id, founder_id=founder, text="secret",
    )
    ok = await mem.forget(
        business_id=biz_a, founder_id=founder, memory_id=entry.id,
    )
    assert ok is False
    # Row still exists
    assert session.get(LongTermMemoryEntry, UUID(entry.id)) is not None


@pytest.mark.asyncio
async def test_forget_handles_garbage_id(session: Session) -> None:
    biz, founder = _seed(session)
    mem = DbLongTermMemory(session)
    ok = await mem.forget(
        business_id=biz, founder_id=founder,
        memory_id="not-a-uuid",
    )
    assert ok is False


# ---- skills ----


@pytest.mark.asyncio
async def test_remember_skill_persists(session: Session) -> None:
    biz_id, founder_id = _seed(session)
    business = session.get(Business, biz_id)
    founder = session.get(Founder, founder_id)
    from korpha.skills.memory import MemoryRememberSkill
    from korpha.skills.types import SkillContext

    skill = MemoryRememberSkill()
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=None,
    )
    result = await skill.run(
        ctx=ctx,
        args={
            "text": "Mike targets freelance designers",
            "tags": "niche, target-customer",
        },
    )
    assert result.payload["text"] == "Mike targets freelance designers"
    assert result.payload["tags"] == ["niche", "target-customer"]
    # Memory got persisted
    rows = list(session.exec(
        __import__("sqlmodel").select(LongTermMemoryEntry)
    ).all())
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_recall_skill_returns_results(session: Session) -> None:
    biz_id, founder_id = _seed(session)
    business = session.get(Business, biz_id)
    founder = session.get(Founder, founder_id)
    from korpha.skills.memory import (
        MemoryRecallSkill, MemoryRememberSkill,
    )
    from korpha.skills.types import SkillContext

    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=None,
    )
    remember = MemoryRememberSkill()
    await remember.run(ctx=ctx, args={
        "text": "Pricing tier is $29/month",
    })
    recall = MemoryRecallSkill()
    out = await recall.run(ctx=ctx, args={"query": "pricing"})
    assert len(out.payload["results"]) == 1
    assert "29" in out.payload["results"][0]["text"]


@pytest.mark.asyncio
async def test_remember_skill_rejects_empty_text(
    session: Session,
) -> None:
    biz_id, founder_id = _seed(session)
    business = session.get(Business, biz_id)
    founder = session.get(Founder, founder_id)
    from korpha.skills.memory import MemoryRememberSkill
    from korpha.skills.types import SkillContext, SkillError

    skill = MemoryRememberSkill()
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=None,
    )
    with pytest.raises(SkillError, match="text"):
        await skill.run(ctx=ctx, args={"text": "   "})


@pytest.mark.asyncio
async def test_recall_skill_caps_limit(session: Session) -> None:
    """Limit is clamped to [1, 50] so a hostile/fat-fingered request
    can't pull thousands of rows into the prompt."""
    biz_id, founder_id = _seed(session)
    business = session.get(Business, biz_id)
    founder = session.get(Founder, founder_id)
    from korpha.skills.memory import (
        MemoryRecallSkill, MemoryRememberSkill,
    )
    from korpha.skills.types import SkillContext

    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=None,
    )
    remember = MemoryRememberSkill()
    for i in range(60):
        await remember.run(ctx=ctx, args={"text": f"note {i} freelance"})
    recall = MemoryRecallSkill()
    out = await recall.run(
        ctx=ctx, args={"query": "freelance", "limit": 9999},
    )
    assert len(out.payload["results"]) <= 50


@pytest.mark.asyncio
async def test_recall_skill_uses_active_plugin_when_registered(
    session: Session,
) -> None:
    """When a plugin registers a non-Noop provider, the skill
    routes through it instead of the DB default."""
    from korpha.memory import memory_registry
    from korpha.memory.contract import LongTermMemory
    from korpha.skills.memory import _resolve_memory
    from korpha.skills.types import SkillContext

    class _Spy(LongTermMemory):
        name = "spy"
        async def add(self, **kw):
            from korpha.memory import MemoryEntry
            return MemoryEntry(
                id="x", text=kw["text"],
                business_id=kw["business_id"],
                founder_id=kw["founder_id"],
            )
        async def search(self, query):
            return []
        async def forget(self, **kw):
            return False
        async def close(self):
            return None

    spy = _Spy()
    memory_registry.set_active(spy, plugin_name="test")
    try:
        biz_id, founder_id = _seed(session)
        business = session.get(Business, biz_id)
        founder = session.get(Founder, founder_id)
        ctx = SkillContext(
            business=business, founder=founder, session=session,
            cost_tracker=None,
        )
        resolved = _resolve_memory(ctx)
        assert resolved is spy
    finally:
        memory_registry.reset_to_noop()
