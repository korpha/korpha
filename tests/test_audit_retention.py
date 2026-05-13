"""Tests for audit log retention — archive + delete + breakdown."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from korpha.audit.model import (
    Activity, ActorType, Cost, InferenceTier,
)
from korpha.audit.retention import (
    archive_activity, archive_all, archive_cost, archive_size_breakdown,
)
from korpha.business.model import Business


def _seed_business(session: Session) -> Business:
    from korpha.identity.model import Founder
    f = Founder(email="x@y.com", display_name="Mike")
    session.add(f); session.commit(); session.refresh(f)
    b = Business(founder_id=f.id, name="WidgetCo", description="t")
    session.add(b); session.commit(); session.refresh(b)
    return b


def _make_activity(
    session: Session, business: Business, *,
    days_ago: int, event_type: str = "test.event",
) -> Activity:
    row = Activity(
        business_id=business.id,
        actor_type=ActorType.AGENT,
        actor_id=uuid4(),
        event_type=event_type,
        payload={"k": "v"},
    )
    session.add(row); session.commit(); session.refresh(row)
    # Backdate after insert to bypass the timestamp default
    row.created_at = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    session.add(row); session.commit(); session.refresh(row)
    return row


def _make_cost(
    session: Session, business: Business, *,
    days_ago: int, cost_usd: float = 0.001,
) -> Cost:
    row = Cost(
        business_id=business.id,
        provider="ollama-cloud",
        model="deepseek-v4-flash",
        tier=InferenceTier.WORKHORSE,
        input_tokens=100,
        output_tokens=200,
        cost_usd=Decimal(str(cost_usd)),
    )
    session.add(row); session.commit(); session.refresh(row)
    row.created_at = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    session.add(row); session.commit(); session.refresh(row)
    return row


# ---- archive_activity ----


def test_archive_activity_writes_jsonl_gz(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    _make_activity(session, biz, days_ago=200)
    _make_activity(session, biz, days_ago=10)  # within window

    stats = archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )
    assert stats.rows_archived == 1
    assert stats.bytes_written > 0
    assert len(stats.months_touched) == 1

    # Archive file is parseable
    files = list(tmp_path.glob("activity-*.jsonl.gz"))
    assert len(files) == 1
    with gzip.open(files[0], "rb") as f:
        content = f.read().decode("utf-8")
    line = content.strip().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["event_type"] == "test.event"
    assert "id" in parsed and "created_at" in parsed


def test_archive_activity_deletes_db_rows_by_default(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    _make_activity(session, biz, days_ago=200)
    _make_activity(session, biz, days_ago=10)

    archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )
    remaining = list(session.exec(select(Activity)).all())
    assert len(remaining) == 1  # only the recent one


def test_archive_activity_dry_run_keeps_db_rows(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    _make_activity(session, biz, days_ago=200)
    _make_activity(session, biz, days_ago=200)

    stats = archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
        delete_after=False,
    )
    assert stats.rows_archived == 2
    # All rows still in DB
    assert len(list(session.exec(select(Activity)).all())) == 2


def test_archive_activity_groups_by_month(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    # Three rows spread across months
    _make_activity(session, biz, days_ago=200)  # ~6.5 months ago
    _make_activity(session, biz, days_ago=240)  # ~8 months ago
    _make_activity(session, biz, days_ago=280)  # ~9 months ago

    stats = archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )
    assert stats.rows_archived == 3
    # Should write to 2-3 separate month files
    files = list(tmp_path.glob("activity-*.jsonl.gz"))
    assert len(files) >= 2


def test_archive_activity_no_old_rows_is_noop(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    _make_activity(session, biz, days_ago=10)
    stats = archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )
    assert stats.rows_archived == 0
    assert list(tmp_path.iterdir()) == []


def test_archive_activity_idempotent_appends(
    session: Session, tmp_path: Path,
) -> None:
    """Running archive twice for the same window doesn't duplicate
    archived data — the second run finds no rows in DB to archive."""
    biz = _seed_business(session)
    _make_activity(session, biz, days_ago=200)

    archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )
    second = archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )
    assert second.rows_archived == 0


def test_archive_activity_appends_to_existing_month(
    session: Session, tmp_path: Path,
) -> None:
    """Two archive runs against rows in the SAME month should both
    end up readable in the JSONL (one row per line)."""
    biz = _seed_business(session)
    _make_activity(
        session, biz, days_ago=200, event_type="first",
    )
    archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )
    _make_activity(
        session, biz, days_ago=200, event_type="second",
    )
    archive_activity(
        session, days_keep=180, archive_dir=tmp_path,
    )

    files = list(tmp_path.glob("activity-*.jsonl.gz"))
    assert len(files) == 1
    with gzip.open(files[0], "rb") as f:
        lines = f.read().decode("utf-8").strip().splitlines()
    event_types = {json.loads(l)["event_type"] for l in lines}
    assert event_types == {"first", "second"}


# ---- archive_cost ----


def test_archive_cost_writes_decimals_as_strings(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    _make_cost(session, biz, days_ago=200, cost_usd=0.0123)

    stats = archive_cost(
        session, days_keep=180, archive_dir=tmp_path,
    )
    assert stats.rows_archived == 1
    files = list(tmp_path.glob("cost-*.jsonl.gz"))
    with gzip.open(files[0], "rb") as f:
        line = f.read().decode("utf-8").strip().splitlines()[0]
    parsed = json.loads(line)
    # Decimal serialized as string to preserve precision (SQLite
    # may pad trailing zeros to the column's decimal_places).
    assert Decimal(parsed["cost_usd"]) == Decimal("0.0123")
    assert parsed["tier"] == "workhorse"


def test_archive_cost_deletes_old_rows(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    _make_cost(session, biz, days_ago=200)
    _make_cost(session, biz, days_ago=10)

    archive_cost(session, days_keep=180, archive_dir=tmp_path)
    remaining = list(session.exec(select(Cost)).all())
    assert len(remaining) == 1


# ---- archive_all ----


def test_archive_all_returns_per_table_stats(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed_business(session)
    _make_activity(session, biz, days_ago=200)
    _make_cost(session, biz, days_ago=200)

    out = archive_all(session, days_keep=180, archive_dir=tmp_path)
    assert out["activity"].rows_archived == 1
    assert out["cost"].rows_archived == 1


# ---- archive_size_breakdown ----


def test_archive_size_breakdown_lists_files(
    session: Session, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    biz = _seed_business(session)
    _make_activity(session, biz, days_ago=200)
    archive_activity(session, days_keep=180)

    bd = archive_size_breakdown()
    assert bd["total_bytes"] > 0
    assert any(
        f["name"].startswith("activity-") for f in bd["files"]
    )


def test_archive_size_breakdown_empty_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    bd = archive_size_breakdown()
    assert bd["total_bytes"] == 0
    assert bd["files"] == []


# ---- CLI ----


@pytest.fixture
def cli_runner_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, Path]:
    from sqlmodel import SQLModel, create_engine
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        biz = _seed_business(s)
        _make_activity(s, biz, days_ago=200)
        _make_cost(s, biz, days_ago=200)
        _make_activity(s, biz, days_ago=10)
    return CliRunner(), tmp_path


def test_cli_audit_archive(
    cli_runner_db: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = cli_runner_db
    from korpha.cli import app
    result = cli_runner.invoke(app, ["audit", "archive"])
    assert result.exit_code == 0, result.stdout
    assert "Activity:" in result.stdout
    assert "Cost:" in result.stdout
    # Archive files written
    files = list((tmp / "archive").glob("*.jsonl.gz"))
    assert len(files) >= 2  # one activity, one cost


def test_cli_audit_archive_dry_run(
    cli_runner_db: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = cli_runner_db
    from korpha.cli import app
    result = cli_runner.invoke(app, ["audit", "archive", "--dry-run"])
    assert result.exit_code == 0
    assert "--dry-run" in result.stdout


def test_cli_audit_archive_invalid_days(
    cli_runner_db: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = cli_runner_db
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "audit", "archive", "--days-keep", "0",
    ])
    assert result.exit_code == 1
    assert "must be" in result.stdout
