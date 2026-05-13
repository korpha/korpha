"""Long-term memory contract: ABC + Noop default + registry."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryEntry:
    """One stored memory. Plugins return these from ``search()``."""

    id: str
    """Provider-specific identifier. Used by ``forget()``."""

    text: str
    """The memory content. Free-form natural language."""

    business_id: UUID
    founder_id: UUID
    tags: tuple[str, ...] = field(default_factory=tuple)
    """Optional categorization (e.g. ``("niche", "stripe-setup")``).
    Plugins may use these for filtering."""

    score: float | None = None
    """Relevance score from a search call. None for direct gets."""

    created_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )

    metadata: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras. Don't depend on shape."""

    namespace_id: UUID | None = None
    """PR-INT-2: BusinessUnit memory namespace this entry belongs to.
    None on pre-PR9 entries; the recall skill filters by namespace
    post-search so the partition holds even when the provider lacks
    native namespace awareness."""


@dataclass(frozen=True)
class MemoryQuery:
    """Search shape passed to ``LongTermMemory.search``."""

    business_id: UUID
    founder_id: UUID
    text: str
    """Free-form query. Provider's job to embed / fuzz / FTS as it sees fit."""

    limit: int = 10
    tags: tuple[str, ...] = field(default_factory=tuple)
    """Optional filter — return entries that match ANY of these tags."""


class LongTermMemory(ABC):
    """ABC for cross-session recall backends."""

    name: str
    """Stable identifier ("mem0", "supermemory", "noop"). Used in
    logging + plugin attribution."""

    @abstractmethod
    async def add(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        text: str,
        tags: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
        namespace_id: UUID | None = None,
    ) -> MemoryEntry:
        """Store a memory. Returns the entry with its provider-
        assigned ``id`` populated. Implementations are free to
        deduplicate / summarize / expand — the agent doesn't
        promise the returned entry's text matches the input
        verbatim.

        ``namespace_id`` (PR-INT-2 partition): the BusinessUnit
        memory namespace the entry belongs to. Defaults to None
        (company-wide) for backward compatibility; the
        memory.remember skill stamps it from the caller's unit
        context."""

    @abstractmethod
    async def search(self, query: MemoryQuery) -> list[MemoryEntry]:
        """Retrieve memories relevant to ``query.text``, scoped to
        the business + founder. Empty list when nothing matches."""

    @abstractmethod
    async def forget(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        memory_id: str,
    ) -> bool:
        """Drop a stored memory. Returns True on success, False
        when the id isn't recognized."""

    @abstractmethod
    async def close(self) -> None:
        """Release any persistent state (HTTP clients, file handles).
        Idempotent."""


class NoopLongTermMemory(LongTermMemory):
    """Default no-op provider. Installs without a memory plugin
    don't crash on calls — they just don't get recall.

    Returns empty / synthetic results so callers can use the
    contract uniformly without ``if memory is not None`` guards.
    """

    name: str = "noop"

    async def add(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        text: str,
        tags: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
        namespace_id: UUID | None = None,
    ) -> MemoryEntry:
        # Return a synthetic entry so the caller's "I just stored
        # this" message has an id to log. The id is throwaway —
        # forget() always returns False against the noop store.
        return MemoryEntry(
            id=f"noop-{uuid4().hex[:8]}",
            text=text,
            business_id=business_id,
            founder_id=founder_id,
            tags=tuple(tags),
            metadata=dict(metadata or {}),
            namespace_id=namespace_id,
        )

    async def search(self, query: MemoryQuery) -> list[MemoryEntry]:
        return []

    async def forget(
        self,
        *,
        business_id: UUID,
        founder_id: UUID,
        memory_id: str,
    ) -> bool:
        return False

    async def close(self) -> None:
        return None


# ---- registry -----------------------------------------------------


class MemoryRegistry:
    """Tracks the active long-term memory provider. Single-active
    semantics: only one provider is in effect at a time. If multiple
    plugins register, the last one wins (logged so the operator
    knows what's active). Prevents the agent from getting
    contradictory memories from competing backends."""

    def __init__(self) -> None:
        self._active: LongTermMemory = NoopLongTermMemory()

    def set_active(
        self, provider: LongTermMemory, *, plugin_name: str = "",
    ) -> None:
        if not isinstance(self._active, NoopLongTermMemory):
            logger.warning(
                "memory: replacing active provider %r with %r "
                "(plugin %s) — only one provider can be active "
                "at a time",
                self._active.name, provider.name, plugin_name,
            )
        self._active = provider

    def active(self) -> LongTermMemory:
        return self._active

    def reset_to_noop(self) -> None:
        """Tests use this to drop a registered provider between cases."""
        self._active = NoopLongTermMemory()


memory_registry = MemoryRegistry()


def active_long_term_memory() -> LongTermMemory:
    """Convenience accessor for callers that don't want to import
    the registry directly."""
    return memory_registry.active()


def set_active_provider(
    provider: LongTermMemory, *, plugin_name: str = "",
) -> None:
    """Convenience wrapper for ``memory_registry.set_active``."""
    memory_registry.set_active(provider, plugin_name=plugin_name)


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
