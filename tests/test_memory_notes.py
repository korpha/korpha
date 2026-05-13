"""Tests for the bounded MEMORY/USER notes layer + auto-injection."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.memory.notes import (
    FounderNote, FounderNoteService, MEMORY_STORE, NoteCapacityError,
    NoteNotFound, USER_STORE,
)
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


# ---- service: add / list ----


def test_add_persists_note(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    note = svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="Project uses Python 3.12 + uv",
    )
    assert note.id is not None
    assert note.store == "memory"
    assert note.content == "Project uses Python 3.12 + uv"


def test_add_strips_whitespace(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    note = svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="   leading + trailing   ",
    )
    assert note.content == "leading + trailing"


def test_add_blank_raises(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    with pytest.raises(ValueError, match="content required"):
        svc.add(
            business_id=business.id, founder_id=founder.id,
            store="memory", content="   ",
        )


def test_add_unknown_store_raises(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    with pytest.raises(ValueError, match="unknown store"):
        svc.add(
            business_id=business.id, founder_id=founder.id,
            store="bogus",  # type: ignore[arg-type]
            content="x",
        )


def test_add_duplicate_returns_existing(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    first = svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="dupe",
    )
    second = svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="dupe",
    )
    assert first.id == second.id  # no second row written


def test_add_capacity_error_on_overflow(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    # Fill memory store to ~98% with junk
    big = "x" * 2100
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content=big,
    )
    with pytest.raises(NoteCapacityError, match="2100/2200"):
        svc.add(
            business_id=business.id, founder_id=founder.id,
            store="memory", content="y" * 200,  # would push to 2300
        )


def test_list_isolates_by_store(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="agent note",
    )
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="user pref",
    )
    mem = svc.list(
        business_id=business.id, founder_id=founder.id, store="memory",
    )
    usr = svc.list(
        business_id=business.id, founder_id=founder.id, store="user",
    )
    assert len(mem) == 1
    assert mem[0].content == "agent note"
    assert len(usr) == 1
    assert usr[0].content == "user pref"


def test_list_isolates_by_business(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Cross-business memory must not leak."""
    from uuid import uuid4
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="ours",
    )
    other_biz = uuid4()
    # bypass FK validation for the test by writing directly
    foreign = FounderNote(
        business_id=other_biz, founder_id=founder.id,
        store="memory", content="theirs",
    )
    session.add(foreign); session.commit()
    rows = svc.list(
        business_id=business.id, founder_id=founder.id, store="memory",
    )
    assert {r.content for r in rows} == {"ours"}


# ---- service: replace ----


def test_replace_finds_unique_substring(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="Mike prefers dark mode in editors",
    )
    updated = svc.replace(
        business_id=business.id, founder_id=founder.id,
        store="user", old_text="dark mode",
        content="Mike prefers light mode in VS Code, dark in terminal",
    )
    assert "light mode" in updated.content


def test_replace_no_match_raises(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="something",
    )
    with pytest.raises(NoteNotFound, match="no entry"):
        svc.replace(
            business_id=business.id, founder_id=founder.id,
            store="memory", old_text="missing", content="x",
        )


def test_replace_ambiguous_substring_raises(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="Project alpha uses X",
    )
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="Project beta uses Y",
    )
    with pytest.raises(NoteNotFound, match="matches 2"):
        svc.replace(
            business_id=business.id, founder_id=founder.id,
            store="memory", old_text="Project", content="x",
        )


def test_replace_capacity_check(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Replacing with a much larger entry that would overflow is rejected."""
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="x" * 1000,
    )
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="short",
    )
    with pytest.raises(NoteCapacityError):
        svc.replace(
            business_id=business.id, founder_id=founder.id,
            store="user", old_text="short", content="y" * 500,
        )  # 1000 + 500 > 1375


# ---- service: remove ----


def test_remove_drops_matching_entry(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="goodbye soon",
    )
    svc.remove(
        business_id=business.id, founder_id=founder.id,
        store="memory", old_text="goodbye",
    )
    rows = svc.list(
        business_id=business.id, founder_id=founder.id, store="memory",
    )
    assert rows == []


def test_remove_no_match_raises(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    with pytest.raises(NoteNotFound):
        svc.remove(
            business_id=business.id, founder_id=founder.id,
            store="memory", old_text="ghost",
        )


# ---- render block ----


def test_render_empty_returns_empty_string(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    assert svc.render_block(
        business_id=business.id, founder_id=founder.id,
    ) == ""


def test_render_includes_both_stores_with_headers(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="Project facts go here",
    )
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="Mike prefers concise replies",
    )
    block = svc.render_block(
        business_id=business.id, founder_id=founder.id,
    )
    assert "AGENT MEMORY" in block
    assert "USER PROFILE" in block
    assert "Project facts" in block
    assert "Mike prefers concise" in block
    # Usage % shown
    assert "%" in block


def test_render_uses_section_delimiter(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="entry one",
    )
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="entry two",
    )
    block = svc.render_block(
        business_id=business.id, founder_id=founder.id,
    )
    assert "§" in block
    assert "entry one" in block
    assert "entry two" in block


def test_render_skips_empty_stores(
    session: Session, business: Business, founder: Founder,
) -> None:
    """If only one store has content, the empty one isn't rendered."""
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="just a user pref",
    )
    block = svc.render_block(
        business_id=business.id, founder_id=founder.id,
    )
    assert "USER PROFILE" in block
    assert "AGENT MEMORY" not in block


# ---- CEO auto-injection ----


def test_ceo_build_messages_injects_notes(
    session: Session, business: Business, founder: Founder,
) -> None:
    """The bounded notes block lands in the CEO system prompt."""
    from korpha.approvals.gate import ApprovalGate
    from korpha.cofounder.ceo import CEO
    from korpha.cofounder.hiring import HiringService
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="Mike speaks German natively",
    )

    pool = InferencePool(providers=[], accounts=[])
    ceo = CEO(
        session=session,
        cost_tracker=CostTracker(pool=pool),
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    msgs = ceo._build_messages(
        business=business, founder=founder, history=[],
        user_message="hi",
    )
    system = msgs[0].content
    assert "Mike speaks German natively" in system
    assert "USER PROFILE" in system


def test_ceo_build_messages_skips_block_when_empty(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Brand-new install: no notes yet → no padded headers."""
    from korpha.approvals.gate import ApprovalGate
    from korpha.cofounder.ceo import CEO
    from korpha.cofounder.hiring import HiringService
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    pool = InferencePool(providers=[], accounts=[])
    ceo = CEO(
        session=session,
        cost_tracker=CostTracker(pool=pool),
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    msgs = ceo._build_messages(
        business=business, founder=founder, history=[],
        user_message="hi",
    )
    system = msgs[0].content
    assert "AGENT MEMORY" not in system
    assert "USER PROFILE" not in system


# ---- agent skill ----


def _ctx(session, business, founder):
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    pool = InferencePool(providers=[], accounts=[])
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=pool),
    )


@pytest.mark.asyncio
async def test_skill_add_writes_note(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["memory.note"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "action": "add", "store": "user",
            "content": "Mike runs on Termux",
        },
    )
    assert result.payload["store"] == "user"
    rows = FounderNoteService(session).list(
        business_id=business.id, founder_id=founder.id, store="user",
    )
    assert any("Termux" in r.content for r in rows)


@pytest.mark.asyncio
async def test_skill_add_capacity_error_surfaces(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="x" * 2100,
    )
    skill = default_registry.skills["memory.note"]
    with pytest.raises(SkillError, match="exceed"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"action": "add", "store": "memory", "content": "y" * 200},
        )


@pytest.mark.asyncio
async def test_skill_replace(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="prefers dark mode",
    )
    skill = default_registry.skills["memory.note"]
    await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "action": "replace", "store": "user",
            "old_text": "dark", "content": "prefers light mode",
        },
    )
    rows = svc.list(
        business_id=business.id, founder_id=founder.id, store="user",
    )
    assert rows[0].content == "prefers light mode"


@pytest.mark.asyncio
async def test_skill_remove(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="memory", content="trash me",
    )
    skill = default_registry.skills["memory.note"]
    await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "action": "remove", "store": "memory",
            "old_text": "trash",
        },
    )
    rows = svc.list(
        business_id=business.id, founder_id=founder.id, store="memory",
    )
    assert rows == []


@pytest.mark.asyncio
async def test_skill_list_returns_entries(
    session: Session, business: Business, founder: Founder,
) -> None:
    svc = FounderNoteService(session)
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="A",
    )
    svc.add(
        business_id=business.id, founder_id=founder.id,
        store="user", content="B",
    )
    skill = default_registry.skills["memory.note"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"action": "list", "store": "user"},
    )
    contents = {e["content"] for e in result.payload["entries"]}
    assert contents == {"A", "B"}
    assert result.payload["limit"] == USER_STORE.char_limit


@pytest.mark.asyncio
async def test_skill_invalid_action_rejected(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["memory.note"]
    with pytest.raises(SkillError, match="action must"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"action": "ponder", "store": "memory"},
        )


@pytest.mark.asyncio
async def test_skill_invalid_store_rejected(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["memory.note"]
    with pytest.raises(SkillError, match="store must"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "action": "add", "store": "bogus",
                "content": "x",
            },
        )


def test_memory_note_skill_is_registered() -> None:
    assert "memory.note" in default_registry.skills
