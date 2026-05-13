"""Best-effort additive auto-migration.

When a model gains a new column (nullable, no default constraint),
``SQLModel.metadata.create_all()`` does NOT add it to an existing table —
it only creates *missing tables*. Without intervention, every additive
schema change breaks every Mike's install on the next ``git pull``.

This module bridges the gap with the simplest possible automigration:
compare each table's actual columns against the model's columns, and
issue ``ALTER TABLE ... ADD COLUMN`` for every nullable column that's
missing. Runs at server startup after ``create_all``.

Limitations (deliberate — keeps the helper trivial):

- Adds only. Never drops columns, renames, changes types, or adjusts
  constraints. Those need a real migration (Alembic) — flag at planning
  time, not here.
- Requires new columns to be nullable. If you must add a NOT NULL column
  to an existing table, write a proper migration that backfills.
- SQLite + Postgres only — both support ``ALTER TABLE ADD COLUMN``.

This is a stopgap, not a substitute for Alembic. Once the schema starts
needing renames / type changes / FK additions, swap to Alembic.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import SQLModel

log = logging.getLogger(__name__)


def _column_sql_type(column: Any) -> str:
    """Render a SQLAlchemy Column type as SQL for ALTER TABLE."""
    return column.type.compile(dialect=None) if hasattr(column.type, "compile") else str(column.type)


def add_missing_columns(engine: Engine) -> list[str]:
    """For every registered table, ADD COLUMN any nullable column that
    is in the model but not in the DB. Returns a list of SQL statements
    actually executed (empty when nothing to do)."""
    import korpha.db.registry  # noqa: F401 — register all models

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []

    for table in SQLModel.metadata.tables.values():
        if table.name not in existing_tables:
            # create_all should have built this — skip; we don't try to
            # create tables here.
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            if not col.nullable:
                log.warning(
                    "auto_schema: %s.%s is NOT NULL — skipping (needs a "
                    "real migration with backfill)",
                    table.name, col.name,
                )
                continue
            try:
                col_type = col.type.compile(engine.dialect)
            except Exception:  # noqa: BLE001
                col_type = str(col.type)
            stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                applied.append(stmt)
                log.info("auto_schema applied: %s", stmt)
            except (OperationalError, ProgrammingError) as exc:
                log.warning(
                    "auto_schema failed for %s.%s: %s",
                    table.name, col.name, exc,
                )

    return applied


__all__ = ["add_missing_columns"]
