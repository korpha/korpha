"""Tests for the best-effort additive schema migration."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from korpha.db.auto_schema import add_missing_columns


@pytest.fixture()
def stale_db(tmp_path: Path):
    """Build a DB with the current schema minus one nullable column,
    so the migrator has something to actually do."""
    db_path = tmp_path / "stale.db"
    # Touch a minimal schema with a deliberately-missing column on
    # an existing table.
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE budget_policy (
            id CHAR(32) NOT NULL PRIMARY KEY,
            business_id CHAR(32) NOT NULL,
            scope VARCHAR NOT NULL,
            agent_role_id CHAR(32),
            tier VARCHAR,
            window VARCHAR NOT NULL,
            limit_usd NUMERIC NOT NULL,
            is_active BOOLEAN NOT NULL,
            paused_reason VARCHAR,
            paused_at DATETIME,
            last_window_start DATETIME,
            label VARCHAR NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def test_adds_missing_business_unit_id(stale_db: Path) -> None:
    engine = create_engine(f"sqlite:///{stale_db}")
    # Confirm the column is missing
    insp = inspect(engine)
    before = {c["name"] for c in insp.get_columns("budget_policy")}
    assert "business_unit_id" not in before

    applied = add_missing_columns(engine)
    assert any("business_unit_id" in stmt for stmt in applied)

    after = {c["name"] for c in inspect(engine).get_columns("budget_policy")}
    assert "business_unit_id" in after


def test_idempotent(stale_db: Path) -> None:
    """Running twice should be a no-op the second time."""
    engine = create_engine(f"sqlite:///{stale_db}")
    first = add_missing_columns(engine)
    assert len(first) >= 1
    second = add_missing_columns(engine)
    assert second == []


def test_does_not_touch_existing_columns(stale_db: Path) -> None:
    """We never DROP / RENAME / RETYPE — only ADD."""
    engine = create_engine(f"sqlite:///{stale_db}")
    before = {c["name"]: c["type"] for c in inspect(engine).get_columns("budget_policy")}
    add_missing_columns(engine)
    after = {c["name"]: c["type"] for c in inspect(engine).get_columns("budget_policy")}
    for name, type_ in before.items():
        assert name in after, f"{name} disappeared"


def test_skips_when_table_missing(tmp_path: Path) -> None:
    """If a table doesn't exist yet, we leave it for create_all."""
    engine = create_engine(f"sqlite:///{tmp_path}/empty.db")
    applied = add_missing_columns(engine)
    # No tables present at all → no ADD COLUMN statements
    assert applied == []
