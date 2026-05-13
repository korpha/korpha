"""Outbound notification channels.

A ``Notifier`` is a one-way send surface (email, SMS, push) — distinct
from a ``ChannelAdapter`` which is a bidirectional conversation
(Telegram, Discord, web). Use Notifiers for digests, blocker alerts,
and other "Mike doesn't reply, he just needs to know" moments.
"""
from korpha.notifications.base import (
    Notification,
    Notifier,
    NotifierError,
)
from korpha.notifications.email import (
    MockEmailNotifier,
    ResendEmailNotifier,
)

__all__ = [
    "MockEmailNotifier",
    "Notification",
    "Notifier",
    "NotifierError",
    "ResendEmailNotifier",
]
