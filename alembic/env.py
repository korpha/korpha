"""Alembic environment for Korpha.

Reads the DB URL from `KORPHA_DB_URL` (the same env var the runtime
uses) so `alembic upgrade head` works against your real DB without an
extra config flag. Falls back to the alembic.ini value otherwise.

Imports `korpha.db.registry` to ensure every SQLModel table is
registered on the metadata before autogenerate compares schema.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Register every model on SQLModel.metadata.
import korpha.db.registry  # noqa: F401  -- side-effect import

config = context.config

# Allow env-var override of the DB URL so a single alembic.ini works for
# both SQLite (default) and Postgres deployments.
db_url = os.getenv("KORPHA_DB_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-friendly migrations
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
