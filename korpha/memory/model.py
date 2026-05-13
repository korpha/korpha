"""SQLModel for long-term memory entries — used by the default
DB-backed implementation in :mod:`korpha.memory.postgres`."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class LongTermMemoryEntry(SQLModel, table=True):
    """One stored cross-session memory.

    Scope: per-business + per-founder. Multi-tenancy is enforced
    at the query layer — every read filters on both ids.

    Naming: keeping the explicit ``LongTermMemoryEntry`` rather
    than ``Memory`` because ``korpha/cofounder/memory.py``
    already owns the per-thread message-window concept and we
    don't want to overload the term.
    """

    __tablename__ = "long_term_memory_entry"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    founder_id: UUID = Field(foreign_key="founder.id", index=True)
    # PR9: BusinessUnit memory namespace. Hard-isolates recall results
    # so a POD agent never surfaces KDP Romance's reader-survey notes
    # via a cosine-similarity match. Defaults to None for backward-
    # compat; new writes assign the calling agent's unit namespace.
    # Cross-namespace recall requires an active CrossNamespaceRecallGrant.
    namespace_id: UUID | None = Field(
        default=None, index=True,
        description=(
            "BusinessUnit.memory_namespace_id of the owning unit. "
            "Null on pre-PR9 rows; recall queries treat null as "
            "'belongs to the default unit' for back-compat."
        ),
    )

    text: str
    """The memory content. Free-form natural language. Indexed for
    LIKE-based retrieval (future: vector embedding alongside)."""

    tags: list[str] = Field(default_factory=list, sa_column=json_column())
    """Optional labels (e.g. ``['niche', 'stripe-setup']``).
    Plugins / queries may filter on these."""

    extra: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )
    """Provider-specific metadata. The default DB-backed impl
    leaves this empty; a future plugin (mem0 / supermemory) might
    stash provider ids / source URLs / etc."""

    created_at: datetime = timestamp_field(index=True)
