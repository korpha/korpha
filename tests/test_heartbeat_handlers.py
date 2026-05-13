"""Tests for built-in wakeup handlers."""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from korpha.blockers.model import BlockerKind, BlockerUrgency
from korpha.blockers.queue import BlockerQueue, BlockerSubmission
from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import (
    AgentRole,
    Message,
    MessageSenderType,
    Thread,
    ThreadPlatform,
)
from korpha.db._base import utcnow
from korpha.heartbeats.dispatcher import HandlerRegistry, HeartbeatService
from korpha.heartbeats.handlers import ceo_daily_digest, register_builtins
from korpha.heartbeats.model import WakeupKind


@pytest.mark.asyncio
async def test_register_builtins_idempotent() -> None:
    register_builtins()
    register_builtins()  # should not raise


@pytest.mark.asyncio
async def test_ceo_daily_digest_writes_message_when_blockers_exist(
    session: Session, business: Business, ceo: AgentRole
) -> None:
    # Set up a web thread for the CEO so the handler has somewhere to post.
    thread = Thread(
        business_id=business.id,
        founder_id=business.founder_id,
        agent_role_id=ceo.id,
        platform=ThreadPlatform.WEB,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)

    hiring = HiringService(session)
    cmo = hiring.hire(business.id, role_type_for_test(), title="CMO")
    queue = BlockerQueue(session=session)
    queue.submit(
        BlockerSubmission(
            business_id=business.id,
            requesting_agent_role_id=cmo.id,
            title="Need brand colors locked",
            detail="Designer is waiting on hex codes",
            kind=BlockerKind.DECISION,
            urgency=BlockerUrgency.NORMAL,
        )
    )

    # Schedule + tick.
    reg = HandlerRegistry()
    reg.register(WakeupKind.CEO_DAILY_DIGEST.value, ceo_daily_digest)
    svc = HeartbeatService(session=session, registry=reg)
    svc.schedule(
        business_id=business.id,
        kind=WakeupKind.CEO_DAILY_DIGEST.value,
        fire_at=utcnow(),
    )
    result = await svc.tick()
    assert result.fired == 1

    # Verify a message was posted into the CEO's web thread.
    msgs = list(
        session.exec(
            select(Message)
            .where(Message.thread_id == thread.id)
            .where(Message.sender_type == MessageSenderType.AGENT)
        ).all()
    )
    assert len(msgs) == 1
    assert "brand colors" in msgs[0].content.lower()


@pytest.mark.asyncio
async def test_ceo_daily_digest_silent_when_no_blockers(
    session: Session, business: Business, ceo: AgentRole
) -> None:
    thread = Thread(
        business_id=business.id,
        founder_id=business.founder_id,
        agent_role_id=ceo.id,
        platform=ThreadPlatform.WEB,
    )
    session.add(thread)
    session.commit()

    reg = HandlerRegistry()
    reg.register(WakeupKind.CEO_DAILY_DIGEST.value, ceo_daily_digest)
    svc = HeartbeatService(session=session, registry=reg)
    svc.schedule(
        business_id=business.id,
        kind=WakeupKind.CEO_DAILY_DIGEST.value,
        fire_at=utcnow(),
    )
    result = await svc.tick()
    assert result.fired == 1

    # No CMO/agent message produced — only the existing setup messages (none).
    msgs = list(
        session.exec(
            select(Message).where(Message.sender_type == MessageSenderType.AGENT)
        ).all()
    )
    assert msgs == []


def role_type_for_test():  # type: ignore[no-untyped-def]
    """Imported lazily so the conftest fixtures load first."""
    from korpha.cofounder.model import RoleType

    return RoleType.CMO
