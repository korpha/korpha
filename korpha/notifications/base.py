"""Notifier ABC + Notification payload."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class NotifierError(RuntimeError):
    """Transport failure (auth, network, quota). Caller decides retry policy."""


@dataclass(frozen=True)
class Notification:
    """One outbound notification.

    ``html_body`` is optional but encouraged — most modern transports
    (email, push) render rich content. ``text_body`` is the fallback for
    plain-text consumers and SHOULD always be provided.
    """

    to: str
    subject: str
    text_body: str
    html_body: str | None = None
    from_address: str | None = None
    """Override the notifier's default From: identity. Most callers leave
    this unset; the notifier picks up the value from config / env."""


class Notifier(ABC):
    """Abstract base for any outbound notification surface."""

    name: str

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """Deliver the notification. Raise NotifierError on failure."""

    @abstractmethod
    async def close(self) -> None:
        """Release any persistent state. Idempotent."""


__all__ = ["Notification", "Notifier", "NotifierError"]
