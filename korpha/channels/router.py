"""ChannelRouter: pump messages from a ChannelAdapter into the CEO.

For each IncomingMessage we:
  1. Find or create a Thread on the right platform for this Founder.
  2. Persist the Founder's message as a Message row.
  3. Build a memory-composed history (recent + summary + FTS5 hits).
  4. Call CEO.handle() to get the cofounder's response.
  5. Persist the response as a Message row, then push back via adapter.send().

Each (founder, business, platform) gets one continuous thread — same
single-voice-of-the-cofounder rule the web UI uses.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.channels.base import ChannelAdapter, IncomingMessage, OutgoingMessage
from korpha.cofounder.ceo import CEO
from korpha.cofounder.memory import MemoryService
from korpha.cofounder.model import (
    Message as DbMessage,
)
from korpha.cofounder.model import (
    MessageSenderType,
    Thread,
    ThreadPlatform,
    ThreadStatus,
)
from korpha.identity.model import Founder

logger = logging.getLogger(__name__)


CeoFactory = Callable[[Session], CEO]


@dataclass
class ChannelRouter:
    """Drives one ChannelAdapter against one (founder, business)."""

    engine: Engine
    adapter: ChannelAdapter
    ceo_factory: CeoFactory
    """Callable that builds a fresh CEO bound to the given session. Same
    factory used by the CLI / API server, so providers + skills + cost
    tracking are shared."""

    business_id: UUID
    founder_id: UUID

    async def run(self) -> None:
        """Loop forever (or until adapter.close()) handling each incoming
        message. Errors in handling one message don't kill the loop —
        we log and move on."""
        async for incoming in self.adapter.stream():
            try:
                await self._handle_one(incoming)
            except Exception:
                logger.exception("channel router failed handling message")

    async def _handle_one(self, incoming: IncomingMessage) -> None:
        with Session(self.engine) as session:
            founder = session.get(Founder, self.founder_id)
            business = session.get(Business, self.business_id)
            if founder is None or business is None:
                logger.warning(
                    "router: missing founder or business — drop message"
                )
                return
            ceo = self.ceo_factory(session)

            thread = self._ensure_thread(
                session, founder=founder, business=business, incoming=incoming
            )

            # Persist Founder message first so it lands in memory before
            # we ask CEO to respond.
            session.add(
                DbMessage(
                    thread_id=thread.id,
                    sender_type=MessageSenderType.FOUNDER,
                    content=incoming.text,
                )
            )
            session.commit()

            mem = MemoryService(session=session)
            history = mem.compose(
                business_id=business.id,
                founder_id=founder.id,
                query=incoming.text,
                platform=incoming.platform,
            )
            result = await ceo.handle(
                business=business,
                founder=founder,
                founder_message=incoming.text,
                history=history,
                thread_id=thread.id,
            )

            ceo_role = ceo.hiring.ensure_ceo(business.id)
            session.add(
                DbMessage(
                    thread_id=thread.id,
                    sender_type=MessageSenderType.AGENT,
                    sender_role_id=ceo_role.id,
                    content=result.content,
                )
            )
            session.commit()

        # Send outside the DB session — keeps the lock window tight.
        # Fall back to a placeholder when the CEO returned empty content
        # (typically because a thinking model burned its token budget on
        # reasoning) so the Founder isn't left wondering whether the bot
        # is alive.
        out_text = result.content.strip() or (
            "_(I started thinking but didn't produce a final answer — "
            "could you rephrase or add a bit more detail?)_"
        )
        await asyncio.shield(
            self.adapter.send(
                OutgoingMessage(
                    channel_user_id=incoming.channel_user_id,
                    text=out_text,
                )
            )
        )
        logger.info(
            "channel router sent reply (chars=%d, was_empty=%s)",
            len(out_text),
            not result.content.strip(),
        )

    def _ensure_thread(
        self,
        session: Session,
        *,
        founder: Founder,
        business: Business,
        incoming: IncomingMessage,
    ) -> Thread:
        """Find the existing per-platform thread for this Founder, or open one."""
        existing = session.exec(
            select(Thread)
            .where(Thread.business_id == business.id)
            .where(Thread.founder_id == founder.id)
            .where(Thread.platform == incoming.platform)
            .where(Thread.status == ThreadStatus.ACTIVE)
        ).first()
        if existing is not None:
            existing.platform_thread_id = incoming.channel_user_id
            session.add(existing)
            session.commit()
            return existing

        ceo = _ceo_role(session, business)
        thread = Thread(
            business_id=business.id,
            founder_id=founder.id,
            agent_role_id=ceo,
            platform=incoming.platform,
            platform_thread_id=incoming.channel_user_id,
        )
        session.add(thread)
        session.commit()
        session.refresh(thread)
        return thread


def _ceo_role(session: Session, business: Business) -> UUID:
    """Resolve the CEO AgentRole for this business — assumes one exists,
    which is true after `korpha init` runs."""
    from korpha.cofounder.hiring import HiringService

    return HiringService(session).ensure_ceo(business.id).id


_PLATFORM_NAMES: dict[str, ThreadPlatform] = {
    "telegram": ThreadPlatform.TELEGRAM,
    "discord": ThreadPlatform.DISCORD,
    "email": ThreadPlatform.EMAIL,
    "slack": ThreadPlatform.SLACK,
    "whatsapp": ThreadPlatform.WHATSAPP,
    "signal": ThreadPlatform.SIGNAL,
}


def platform_from_name(name: str) -> ThreadPlatform:
    """Map a CLI / config string to ThreadPlatform — case-insensitive."""
    key = name.strip().lower()
    if key not in _PLATFORM_NAMES:
        valid = ", ".join(sorted(_PLATFORM_NAMES))
        raise ValueError(f"unknown channel {name!r}. Valid: {valid}")
    return _PLATFORM_NAMES[key]


__all__ = [
    "CeoFactory",
    "ChannelRouter",
    "platform_from_name",
]
