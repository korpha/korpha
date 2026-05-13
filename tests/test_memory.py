"""MemoryService tests."""
from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.memory import MemoryService
from korpha.cofounder.model import (
    Message,
    MessageSenderType,
    RoleType,
    Thread,
    ThreadPlatform,
)
from korpha.db._base import utcnow
from korpha.identity.model import Founder
from korpha.inference.types import Role


def _setup_thread(
    session: Session, business: Business, founder: Founder, platform: ThreadPlatform
) -> Thread:
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    thread = Thread(
        business_id=business.id,
        founder_id=founder.id,
        agent_role_id=ceo.id,
        platform=platform,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def test_empty_history_when_no_messages(
    session: Session, business: Business, founder: Founder
) -> None:
    mem = MemoryService(session=session)
    assert mem.load_recent(business_id=business.id, founder_id=founder.id) == []


def test_loads_messages_in_chronological_order(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _setup_thread(session, business, founder, ThreadPlatform.WEB)
    base = utcnow()
    for i in range(3):
        msg = Message(
            thread_id=thread.id,
            sender_type=MessageSenderType.FOUNDER if i % 2 == 0 else MessageSenderType.AGENT,
            content=f"message {i}",
        )
        # Force created_at so the order is deterministic regardless of insert lag.
        msg.created_at = base + timedelta(seconds=i)
        session.add(msg)
    session.commit()

    mem = MemoryService(session=session)
    history = mem.load_recent(business_id=business.id, founder_id=founder.id)
    assert [m.content for m in history] == ["message 0", "message 1", "message 2"]


def test_role_mapping_founder_to_user_agent_to_assistant(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _setup_thread(session, business, founder, ThreadPlatform.WEB)
    msgs = [
        Message(thread_id=thread.id, sender_type=MessageSenderType.FOUNDER, content="hi"),
        Message(thread_id=thread.id, sender_type=MessageSenderType.AGENT, content="hi back"),
        Message(thread_id=thread.id, sender_type=MessageSenderType.SYSTEM, content="note"),
    ]
    base = utcnow()
    for i, m in enumerate(msgs):
        m.created_at = base + timedelta(seconds=i)
        session.add(m)
    session.commit()

    mem = MemoryService(session=session)
    history = mem.load_recent(business_id=business.id, founder_id=founder.id)
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT, Role.SYSTEM]


def test_limit_caps_returned_window(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _setup_thread(session, business, founder, ThreadPlatform.WEB)
    base = utcnow()
    for i in range(8):
        msg = Message(
            thread_id=thread.id,
            sender_type=MessageSenderType.FOUNDER,
            content=f"msg {i}",
        )
        msg.created_at = base + timedelta(seconds=i)
        session.add(msg)
    session.commit()

    mem = MemoryService(session=session)
    history = mem.load_recent(
        business_id=business.id, founder_id=founder.id, limit=3
    )
    assert len(history) == 3


def test_platform_filter(
    session: Session, business: Business, founder: Founder
) -> None:
    web_thread = _setup_thread(session, business, founder, ThreadPlatform.WEB)
    # Add a Telegram thread + message
    hiring = HiringService(session)
    ceo = hiring.get_active_role(business.id, RoleType.CEO)
    assert ceo is not None
    tg_thread = Thread(
        business_id=business.id,
        founder_id=founder.id,
        agent_role_id=ceo.id,
        platform=ThreadPlatform.TELEGRAM,
    )
    session.add(tg_thread)
    session.commit()
    session.refresh(tg_thread)

    base = utcnow()
    web_msg = Message(thread_id=web_thread.id, sender_type=MessageSenderType.FOUNDER, content="web only")
    web_msg.created_at = base
    tg_msg = Message(thread_id=tg_thread.id, sender_type=MessageSenderType.FOUNDER, content="telegram only")
    tg_msg.created_at = base + timedelta(seconds=1)
    session.add(web_msg)
    session.add(tg_msg)
    session.commit()

    mem = MemoryService(session=session)
    web_only = mem.load_recent(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
    )
    all_p = mem.load_recent(
        business_id=business.id, founder_id=founder.id
    )
    assert [m.content for m in web_only] == ["web only"]
    assert {m.content for m in all_p} == {"web only", "telegram only"}


def test_max_age_filter(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _setup_thread(session, business, founder, ThreadPlatform.WEB)
    base = utcnow()
    fresh = Message(thread_id=thread.id, sender_type=MessageSenderType.FOUNDER, content="fresh")
    fresh.created_at = base
    stale = Message(thread_id=thread.id, sender_type=MessageSenderType.FOUNDER, content="stale")
    stale.created_at = base - timedelta(hours=48)
    session.add(fresh)
    session.add(stale)
    session.commit()

    mem = MemoryService(session=session)
    history = mem.load_recent(
        business_id=business.id,
        founder_id=founder.id,
        max_age_hours=24,
    )
    assert [m.content for m in history] == ["fresh"]


def test_agent_messages_get_role_name(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _setup_thread(session, business, founder, ThreadPlatform.WEB)
    hiring = HiringService(session)
    ceo = hiring.get_active_role(business.id, RoleType.CEO)
    assert ceo is not None
    msg = Message(
        thread_id=thread.id,
        sender_type=MessageSenderType.AGENT,
        sender_role_id=ceo.id,
        content="from CEO",
    )
    msg.created_at = utcnow()
    session.add(msg)
    session.commit()

    mem = MemoryService(session=session)
    history = mem.load_recent(business_id=business.id, founder_id=founder.id)
    assert history[0].name == "CEO"
