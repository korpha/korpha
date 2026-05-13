"""Channel adapter ABC + message types.

A channel speaks one transport (Telegram, Discord, Email, web, ...). The
adapter's job is to:

  1. ``stream()`` — yield IncomingMessage objects as the Founder posts on
     the channel. Long-running async generator; lifetime tied to whatever
     loop drives it.
  2. ``send(...)`` — push OutgoingMessage to the right user/chat/channel.
  3. ``close()`` — release sockets, file handles, polling tasks.

The router on top of this knows nothing about Telegram-vs-Discord
specifics — it just pulls events and dispatches to the CEO. New channels
plug in by implementing this ABC, no router changes needed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from korpha.cofounder.model import ThreadPlatform
from korpha.db._base import utcnow


class ChannelError(RuntimeError):
    """Transport problem: auth failure, network drop, server rejected payload."""


@dataclass(frozen=True)
class IncomingMessage:
    """One message arriving from a Founder via a channel."""

    platform: ThreadPlatform
    channel_user_id: str
    """Stable per-platform user identifier (Telegram chat_id, Discord user
    snowflake, email From: address). Not necessarily a username — choose the
    most stable handle the platform offers."""

    text: str
    received_at: datetime = field(default_factory=utcnow)
    display_name: str | None = None
    """Best-effort human-readable name for logging. Don't depend on it."""
    raw: dict[str, Any] = field(default_factory=dict)
    """Provider-specific full payload, kept for debug."""


@dataclass(frozen=True)
class OutgoingMessage:
    """One message Korpha is sending out to a Founder."""

    channel_user_id: str
    text: str
    """Markdown-flavored body. Adapters may downgrade to plain text if the
    platform doesn't support formatting."""


class ChannelAdapter(ABC):
    """Abstract base for any one-voice channel transport."""

    platform: ThreadPlatform

    @abstractmethod
    def stream(self) -> AsyncIterator[IncomingMessage]:
        """Return an async iterator yielding incoming messages until close().

        Implementations are typically async generators (``async def stream``
        with ``yield``). The base signature is *not* ``async`` so mypy treats
        the return type as the iterator itself, not a coroutine returning one."""

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


__all__ = [
    "ChannelAdapter",
    "ChannelError",
    "IncomingMessage",
    "OutgoingMessage",
]
