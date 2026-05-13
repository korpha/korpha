"""ConversationRouter — sticky threads + single-voice rule.

Two rules from BRIEF.md / ARCHITECTURE.md:

1. **Sticky threads.** When the Founder initiates a conversation with a C-suite
   agent (e.g. CMO), that thread sticks to that agent until: Founder closes it
   explicitly, ``sticky_ttl_seconds`` (default 24h) elapses, or the Founder
   starts a new topic via the CEO.

2. **Single-voice rule.** Outside of sticky threads, only CEO may proactively
   message the Founder. If a non-CEO agent wants to surface something to the
   Founder, it must route through CEO. CEO consolidates and speaks in one
   voice — Founder is never pinged by 4 different agents in parallel.

The router is platform-aware: a Telegram CMO sticky does not affect the web
app. Each (founder, platform, agent_role) tuple has its own thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import Activity, ActorType
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import (
    AgentRole,
    Message,
    MessageSenderType,
    RoleType,
    Thread,
    ThreadPlatform,
    ThreadStatus,
)
from korpha.db._base import as_utc, utcnow

DEFAULT_STICKY_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class RoutingReason(StrEnum):
    DIRECT_TO_CEO = "direct_to_ceo"
    STICKY_THREAD = "sticky_thread"
    SINGLE_VOICE_RELAY = "single_voice_relay"


@dataclass(frozen=True)
class RoutingDecision:
    thread_id: UUID
    delivering_agent_role_id: UUID
    """Whose voice appears on the message to the Founder."""

    original_requester_role_id: UUID | None
    """If the message was relayed via CEO, this is the agent who asked."""

    reason: RoutingReason


@dataclass
class ConversationRouter:
    session: Session
    hiring: HiringService
    sticky_ttl_seconds: int = DEFAULT_STICKY_TTL_SECONDS
    _now_fn: object = field(default=utcnow)

    def now(self) -> datetime:
        return self._now_fn() if callable(self._now_fn) else utcnow()

    # -- inbound: Founder sends a message --

    def route_inbound(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        platform: ThreadPlatform,
        content: str,
        platform_thread_id: str | None = None,
        force_agent_role_id: UUID | None = None,
    ) -> RoutingDecision:
        """Route a Founder-originated message to the right agent.

        - If ``force_agent_role_id`` set: Founder explicitly DM'd a C-suite
          agent (e.g. clicked "chat with CMO"). Sticky thread starts/refreshes.
        - Else: there's an active sticky thread on this platform → use it.
        - Else: default to CEO.
        """
        active = self._find_active_sticky(business_id, founder_id, platform)

        if force_agent_role_id is not None:
            thread = self._upsert_thread(
                business_id=business_id,
                founder_id=founder_id,
                agent_role_id=force_agent_role_id,
                platform=platform,
                platform_thread_id=platform_thread_id,
            )
            self._refresh_sticky(thread)
            self._append_message(thread, MessageSenderType.FOUNDER, content)
            self._log(
                business_id=business_id,
                actor_id=founder_id,
                event_type="thread.sticky_started",
                payload={"thread_id": str(thread.id), "agent": str(force_agent_role_id)},
            )
            return RoutingDecision(
                thread_id=thread.id,
                delivering_agent_role_id=force_agent_role_id,
                original_requester_role_id=None,
                reason=RoutingReason.STICKY_THREAD,
            )

        if active is not None:
            self._append_message(active, MessageSenderType.FOUNDER, content)
            return RoutingDecision(
                thread_id=active.id,
                delivering_agent_role_id=active.agent_role_id,
                original_requester_role_id=None,
                reason=RoutingReason.STICKY_THREAD,
            )

        # Default: CEO.
        ceo = self.hiring.ensure_ceo(business_id)
        thread = self._upsert_thread(
            business_id=business_id,
            founder_id=founder_id,
            agent_role_id=ceo.id,
            platform=platform,
            platform_thread_id=platform_thread_id,
        )
        self._append_message(thread, MessageSenderType.FOUNDER, content)
        return RoutingDecision(
            thread_id=thread.id,
            delivering_agent_role_id=ceo.id,
            original_requester_role_id=None,
            reason=RoutingReason.DIRECT_TO_CEO,
        )

    # -- outbound: an agent wants to message the Founder --

    def route_outbound(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        platform: ThreadPlatform,
        content: str,
        requesting_agent_role_id: UUID,
        platform_thread_id: str | None = None,
        attachments: dict | None = None,
    ) -> RoutingDecision:
        """Apply the single-voice rule.

        - If the requesting agent owns an active sticky thread on this platform,
          they speak in their own voice in that thread.
        - Otherwise the message is relayed via CEO (single-voice rule).
        """
        requesting = self.session.get(AgentRole, requesting_agent_role_id)
        if requesting is None:
            raise KeyError(f"AgentRole {requesting_agent_role_id} not found")

        sticky = self._find_active_sticky(business_id, founder_id, platform)

        if sticky is not None and sticky.agent_role_id == requesting_agent_role_id:
            self._append_message(
                sticky,
                MessageSenderType.AGENT,
                content,
                sender_role_id=requesting_agent_role_id,
                attachments=attachments,
            )
            return RoutingDecision(
                thread_id=sticky.id,
                delivering_agent_role_id=requesting_agent_role_id,
                original_requester_role_id=None,
                reason=RoutingReason.STICKY_THREAD,
            )

        if requesting.role_type == RoleType.CEO:
            ceo_thread = self._upsert_thread(
                business_id=business_id,
                founder_id=founder_id,
                agent_role_id=requesting_agent_role_id,
                platform=platform,
                platform_thread_id=platform_thread_id,
            )
            self._append_message(
                ceo_thread,
                MessageSenderType.AGENT,
                content,
                sender_role_id=requesting_agent_role_id,
                attachments=attachments,
            )
            return RoutingDecision(
                thread_id=ceo_thread.id,
                delivering_agent_role_id=requesting_agent_role_id,
                original_requester_role_id=None,
                reason=RoutingReason.DIRECT_TO_CEO,
            )

        # Single-voice relay: CEO speaks for them.
        ceo = self.hiring.ensure_ceo(business_id)
        ceo_thread = self._upsert_thread(
            business_id=business_id,
            founder_id=founder_id,
            agent_role_id=ceo.id,
            platform=platform,
            platform_thread_id=platform_thread_id,
        )
        relayed = (
            f"[on behalf of {requesting.title}] {content}"
            if requesting.title
            else content
        )
        self._append_message(
            ceo_thread,
            MessageSenderType.AGENT,
            relayed,
            sender_role_id=ceo.id,
            attachments=attachments,
        )
        self._log(
            business_id=business_id,
            actor_id=ceo.id,
            event_type="thread.single_voice_relay",
            payload={
                "thread_id": str(ceo_thread.id),
                "original_requester": str(requesting_agent_role_id),
                "delivered_by": str(ceo.id),
            },
        )
        return RoutingDecision(
            thread_id=ceo_thread.id,
            delivering_agent_role_id=ceo.id,
            original_requester_role_id=requesting_agent_role_id,
            reason=RoutingReason.SINGLE_VOICE_RELAY,
        )

    # -- sticky thread management --

    def close_thread(self, thread_id: UUID) -> Thread:
        thread = self._require_thread(thread_id)
        thread.status = ThreadStatus.CLOSED
        thread.sticky_until = None
        self.session.add(thread)
        self.session.commit()
        self.session.refresh(thread)
        self._log(
            business_id=thread.business_id,
            actor_id=thread.founder_id,
            event_type="thread.closed",
            payload={"thread_id": str(thread.id)},
        )
        return thread

    def extend_sticky(self, thread_id: UUID, *, ttl_seconds: int | None = None) -> Thread:
        thread = self._require_thread(thread_id)
        ttl = ttl_seconds if ttl_seconds is not None else self.sticky_ttl_seconds
        thread.sticky_until = self.now() + timedelta(seconds=ttl)
        self.session.add(thread)
        self.session.commit()
        self.session.refresh(thread)
        return thread

    def is_sticky_active(self, thread_id: UUID) -> bool:
        thread = self.session.get(Thread, thread_id)
        if thread is None or thread.status != ThreadStatus.ACTIVE:
            return False
        sticky_until = as_utc(thread.sticky_until)
        return sticky_until is not None and self.now() < sticky_until

    # -- helpers --

    def _find_active_sticky(
        self,
        business_id: UUID,
        founder_id: UUID,
        platform: ThreadPlatform,
    ) -> Thread | None:
        now = self.now()
        stmt = (
            select(Thread)
            .where(Thread.business_id == business_id)
            .where(Thread.founder_id == founder_id)
            .where(Thread.platform == platform)
            .where(Thread.status == ThreadStatus.ACTIVE)
            .where(Thread.sticky_until.is_not(None))  # type: ignore[union-attr]
            .order_by(Thread.last_message_at.desc())  # type: ignore[attr-defined]
        )
        for thread in self.session.exec(stmt).all():
            sticky_until = as_utc(thread.sticky_until)
            if sticky_until is not None and now < sticky_until:
                return thread
        return None

    def _upsert_thread(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        agent_role_id: UUID,
        platform: ThreadPlatform,
        platform_thread_id: str | None,
    ) -> Thread:
        stmt = (
            select(Thread)
            .where(Thread.business_id == business_id)
            .where(Thread.founder_id == founder_id)
            .where(Thread.agent_role_id == agent_role_id)
            .where(Thread.platform == platform)
            .where(Thread.status == ThreadStatus.ACTIVE)
        )
        existing = self.session.exec(stmt).first()
        if existing is not None:
            if platform_thread_id is not None and existing.platform_thread_id is None:
                existing.platform_thread_id = platform_thread_id
                self.session.add(existing)
                self.session.commit()
                self.session.refresh(existing)
            return existing

        thread = Thread(
            business_id=business_id,
            founder_id=founder_id,
            agent_role_id=agent_role_id,
            platform=platform,
            platform_thread_id=platform_thread_id,
        )
        self.session.add(thread)
        self.session.commit()
        self.session.refresh(thread)
        return thread

    def _refresh_sticky(self, thread: Thread) -> None:
        thread.sticky_until = self.now() + timedelta(seconds=self.sticky_ttl_seconds)
        self.session.add(thread)
        self.session.commit()
        self.session.refresh(thread)

    def _append_message(
        self,
        thread: Thread,
        sender_type: MessageSenderType,
        content: str,
        *,
        sender_role_id: UUID | None = None,
        attachments: dict | None = None,
    ) -> Message:
        message = Message(
            thread_id=thread.id,
            sender_type=sender_type,
            sender_role_id=sender_role_id,
            content=content,
            attachments=dict(attachments) if attachments else {},
        )
        thread.last_message_at = self.now()
        self.session.add(message)
        self.session.add(thread)
        self.session.commit()
        self.session.refresh(message)
        return message

    def _require_thread(self, thread_id: UUID) -> Thread:
        thread = self.session.get(Thread, thread_id)
        if thread is None:
            raise KeyError(f"Thread {thread_id} not found")
        return thread

    def _log(
        self,
        *,
        business_id: UUID,
        actor_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        self.session.add(
            Activity(
                business_id=business_id,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                event_type=event_type,
                payload=payload,
            )
        )
        self.session.commit()
