"""Database layer: engine, session, and registry helpers.

Importing model modules from their domain folders is the responsibility of
`korpha.db.registry` — this avoids circular imports between the model
files (which depend on `db._base` helpers) and a registry module.
"""
from __future__ import annotations

from korpha.db._session import (
    create_db_and_tables,
    get_engine,
    get_session,
)

__all__ = [
    "create_db_and_tables",
    "get_engine",
    "get_session",
]
