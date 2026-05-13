"""Shared database primitives: mixins, ID factories, JSON column."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field


def utcnow() -> datetime:
    """Timezone-aware UTC now. Always use this — never datetime.utcnow()."""
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime to timezone-aware UTC.

    SQLite stores datetimes without tzinfo, so values loaded from rows come
    back naive even when written as aware. Use this when comparing a stored
    datetime against ``utcnow()`` to avoid TypeError on mixed comparison.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def new_id() -> UUID:
    return uuid4()


def primary_key_field() -> Any:
    return Field(default_factory=new_id, primary_key=True)


def timestamp_field(*, default_factory: Any = utcnow, **kwargs: Any) -> Any:
    return Field(default_factory=default_factory, **kwargs)


def json_column() -> Any:
    """JSON column — JSONB on Postgres, JSON on SQLite (for tests).

    The dialect-aware variant is the production-correct shape: Postgres
    gets JSONB (binary, indexable, smaller), SQLite gets plain JSON.
    Don't downgrade this to bare ``JSON`` — that costs us GIN-index
    eligibility on every JSON query in production.
    """
    return Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
