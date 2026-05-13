"""SQLAlchemy engine and session factory."""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from korpha.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    connect_args: dict[str, object] = {}
    if settings.db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(settings.db_url, connect_args=connect_args, echo=False)


def create_db_and_tables() -> None:
    """Create all registered tables + ADD COLUMN any nullable column
    the model has but the existing DB doesn't. Idempotent. For dev /
    first-run / after a model gains an additive column on `git pull`."""
    import korpha.db.registry  # noqa: F401 — ensures all models are imported

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    import contextlib
    with contextlib.suppress(Exception):
        from korpha.db.auto_schema import add_missing_columns
        add_missing_columns(engine)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
