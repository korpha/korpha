"""Channel adapters: Founder ↔ Korpha over external surfaces.

Channels are interchangeable transports (Telegram, Discord, Email, web, …)
that wrap the same one-voice CEO conversation. Same trust envelope, same
approval gate, same blocker queue — only the message routing changes.

Discovery: built-in adapters self-register with ``platform_registry``
on import. Plugin-supplied adapters do the same via the plugin loader.
The runtime instantiates by name (``platform_registry.create_adapter(
"telegram", config)``) — no if/elif chain on platform.
"""
from korpha.channels.base import (
    ChannelAdapter,
    ChannelError,
    IncomingMessage,
    OutgoingMessage,
)
# Importing the adapter modules below eagerly registers them with
# platform_registry. Order matters only for last-writer-wins
# semantics, which we never rely on for built-ins.
from korpha.channels.email_inbound import ImapEmailAdapter
from korpha.channels.registry import (
    PlatformEntry,
    PlatformRegistry,
    platform_registry,
)
from korpha.channels.router import ChannelRouter
from korpha.channels.telegram import TelegramAdapter

__all__ = [
    "ChannelAdapter",
    "ChannelError",
    "ChannelRouter",
    "ImapEmailAdapter",
    "IncomingMessage",
    "OutgoingMessage",
    "PlatformEntry",
    "PlatformRegistry",
    "TelegramAdapter",
    "platform_registry",
]
