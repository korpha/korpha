"""Pluggable long-term memory backends.

Korpha's per-thread memory (recent message window + summaries) is
in ``korpha/cofounder/memory.py`` + ``summarizer.py``. That handles
"what happened in the last few exchanges" — sufficient for the
conversational loop.

What it doesn't handle: cross-session recall. "Remember that I'm
targeting freelance designers, not enterprise" should survive across
chat sessions and Telegram restarts. Solving that well requires a
proper retrieval store with embeddings + filtering.

Rather than build that ourselves, we expose an ABC and let
third-party plugins (mem0, supermemory, honcho) plug in. The
default implementation is a no-op — installs without a memory
plugin work fine, they just don't get cross-session recall.

Architecture:

  - ``LongTermMemory`` ABC: ``add`` / ``search`` / ``forget``.
    Per-business, per-founder scoping is the caller's
    responsibility (passed as ids).
  - ``NoopLongTermMemory`` — the default. Methods return empty
    results so call sites don't need ``if memory is not None``
    guards.
  - ``MemoryRegistry`` — process-wide singleton. Plugins register
    via ``PluginHost.add_long_term_memory(provider)``; the runtime
    pulls the active provider via ``active_long_term_memory()``.
  - Plugins declare ``long_term_memory`` capability in their
    manifest to register a provider.
"""
from korpha.memory.contract import (
    LongTermMemory,
    MemoryEntry,
    MemoryQuery,
    MemoryRegistry,
    NoopLongTermMemory,
    active_long_term_memory,
    memory_registry,
    set_active_provider,
)
# Import the SQLModel so SQLModel.metadata.create_all picks up the
# table on a fresh DB (matters for dev SQLite installs that don't
# run alembic).
from korpha.memory.model import LongTermMemoryEntry  # noqa: F401

__all__ = [
    "LongTermMemory",
    "MemoryEntry",
    "MemoryQuery",
    "MemoryRegistry",
    "NoopLongTermMemory",
    "active_long_term_memory",
    "memory_registry",
    "set_active_provider",
]
