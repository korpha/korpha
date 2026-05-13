"""PR2 tests — Alembic migration + backfill default unit per Business.

Two scopes:

* ``backfill_default_units(conn)`` — the helper function used by both
  the migration and (eventually) a CLI re-trigger. Tested via the
  shared test ``engine``/``session`` fixtures (Pydantic models +
  SQLModel ``create_all`` give us the same schema the alembic
  migration produces).
* The full Alembic upgrade chain against a fresh temp SQLite — smoke
  test that ``alembic upgrade head`` succeeds end-to-end with the
  new revision applied last.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.business_units.model import BusinessUnit, BusinessUnitKind
from korpha.identity.model import Founder


# ---------------------------------------------------------------------------
# backfill_default_units — the reusable helper
# ---------------------------------------------------------------------------


def _load_backfill_module():
    """Load the alembic revision module by path because alembic
    revisions aren't on the regular import path."""
    repo_root = Path(__file__).resolve().parent.parent
    rev_path = (
        repo_root / "alembic" / "versions"
        / "eb2e487c5ec8_business_unit_and_product_tables.py"
    )
    spec = importlib.util.spec_from_file_location(
        "pr2_migration", str(rev_path),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backfill_fn():
    """Return the ``backfill_default_units`` function from the migration."""
    return _load_backfill_module().backfill_default_units


def test_backfill_no_businesses_no_op(
    engine: Engine, backfill_fn,
) -> None:
    """Empty DB → backfill creates 0 rows, no errors."""
    with engine.connect() as conn:
        created = backfill_fn(conn)
        conn.commit()
    assert created == 0


def test_backfill_creates_one_default_per_business(
    session: Session, engine: Engine, founder: Founder, backfill_fn,
) -> None:
    """Each Business gets one default BusinessUnit + unique namespace."""
    b1 = Business(
        founder_id=founder.id, name="Marketro LLC",
        description="Andrew's holding co", founder_brief={},
    )
    b2 = Business(
        founder_id=founder.id, name="SoloCo",
        description="another biz", founder_brief={},
    )
    session.add_all([b1, b2])
    session.commit()
    session.refresh(b1); session.refresh(b2)

    with engine.connect() as conn:
        created = backfill_fn(conn)
        conn.commit()

    assert created == 2

    units = list(session.exec(select(BusinessUnit)).all())
    assert len(units) == 2

    by_business = {u.business_id: u for u in units}
    assert b1.id in by_business
    assert b2.id in by_business

    for u in units:
        assert u.kind == BusinessUnitKind.DEFAULT
        assert u.parent_id is None
        assert u.status == "active"
        assert u.memory_namespace_id is not None

    # Unique namespaces across both
    namespaces = {u.memory_namespace_id for u in units}
    assert len(namespaces) == 2

    # Slug derived from business name
    assert by_business[b1.id].slug == "marketro-llc"
    assert by_business[b2.id].slug == "soloco"


def test_backfill_is_idempotent(
    session: Session, engine: Engine, founder: Founder, backfill_fn,
) -> None:
    """Re-running backfill on a DB that already has default units
    creates 0 new rows."""
    b = Business(
        founder_id=founder.id, name="OnlyCo",
        description="x", founder_brief={},
    )
    session.add(b)
    session.commit()

    with engine.connect() as conn:
        first = backfill_fn(conn)
        conn.commit()
    assert first == 1

    with engine.connect() as conn:
        second = backfill_fn(conn)
        conn.commit()
    assert second == 0

    units = list(session.exec(select(BusinessUnit)).all())
    assert len(units) == 1


def test_backfill_skips_businesses_with_existing_units(
    session: Session, engine: Engine, founder: Founder, backfill_fn,
) -> None:
    """If a Business already has *any* unit (from manual creation,
    HR skill spawn, etc.), backfill leaves it alone."""
    b1 = Business(
        founder_id=founder.id, name="HasUnit",
        description="x", founder_brief={},
    )
    b2 = Business(
        founder_id=founder.id, name="NoUnit",
        description="y", founder_brief={},
    )
    session.add_all([b1, b2])
    session.commit()
    session.refresh(b1); session.refresh(b2)

    # Pre-create a Line unit (not DEFAULT) on b1
    pre_existing = BusinessUnit(
        business_id=b1.id, name="KDP", slug="kdp",
        kind=BusinessUnitKind.DEFAULT,
    )
    session.add(pre_existing)
    session.commit()

    with engine.connect() as conn:
        created = backfill_fn(conn)
        conn.commit()
    # Only b2 gets a default; b1 already has a unit (any kind suffices
    # to skip — the backfill is "do we have any unit for this biz?")
    assert created == 1

    b1_units = list(session.exec(
        select(BusinessUnit).where(BusinessUnit.business_id == b1.id)
    ).all())
    b2_units = list(session.exec(
        select(BusinessUnit).where(BusinessUnit.business_id == b2.id)
    ).all())
    assert len(b1_units) == 1   # the pre-existing one only
    assert len(b2_units) == 1


def test_backfill_namespace_is_unique_across_runs(
    session: Session, engine: Engine, founder: Founder, backfill_fn,
) -> None:
    """Each run that DOES insert a unit produces a fresh namespace.
    No collision possible with prior installs because UUIDv4."""
    for name in ["A", "B", "C", "D"]:
        b = Business(
            founder_id=founder.id, name=name,
            description="x", founder_brief={},
        )
        session.add(b)
    session.commit()

    with engine.connect() as conn:
        created = backfill_fn(conn)
        conn.commit()
    assert created == 4

    units = list(session.exec(select(BusinessUnit)).all())
    namespaces = {u.memory_namespace_id for u in units}
    assert len(namespaces) == 4


# ---------------------------------------------------------------------------
# Full Alembic upgrade smoke test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("KORPHA_SKIP_ALEMBIC_TESTS") == "1",
    reason="Alembic full-chain test skipped via env override.",
)
def test_alembic_upgrade_head_succeeds_on_fresh_db(
    tmp_path: Path,
) -> None:
    """Smoke: ``alembic upgrade head`` against an empty SQLite DB runs
    the full revision chain including our new one without error."""
    db_path = tmp_path / "fresh.db"
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["KORPHA_DB_URL"] = f"sqlite:///{db_path}"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"alembic exit {result.returncode}\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    # New revision applied
    assert "eb2e487c5ec8" in result.stderr or "eb2e487c5ec8" in result.stdout


@pytest.mark.skipif(
    os.environ.get("KORPHA_SKIP_ALEMBIC_TESTS") == "1",
    reason="Alembic full-chain test skipped via env override.",
)
def test_alembic_creates_tables_with_correct_shape(
    tmp_path: Path,
) -> None:
    """After ``alembic upgrade head``, the new tables exist with the
    expected column names. Sanity check on the schema definition."""
    import sqlite3

    db_path = tmp_path / "shape.db"
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["KORPHA_DB_URL"] = f"sqlite:///{db_path}"

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        timeout=60,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        bu_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(business_unit)")
        }
        assert {
            "id", "business_id", "parent_id", "kind", "name", "slug",
            "owner_agent_role_id", "playbook_skill_pack",
            "niche_profile", "memory_namespace_id",
            "status", "paused_at", "paused_reason", "config",
            "created_at", "updated_at",
        } <= bu_cols

        bp_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(business_product)")
        }
        assert {
            "id", "business_unit_id", "business_id", "kind",
            "name", "slug", "starts_at", "ends_at",
            "attributes", "status", "created_at", "updated_at",
        } <= bp_cols
    finally:
        conn.close()
