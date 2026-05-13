"""Tests for `korpha cron add-vacuum` preset."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from typer.testing import CliRunner

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.scriptcron.model import ScriptCron  # noqa: F401


@pytest.fixture
def runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, Path]:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit()
        b = Business(
            founder_id=f.id, name="WidgetCo", description="t",
        )
        s.add(b); s.commit()
    return CliRunner(), tmp_path


def _read_jobs(tmp_path: Path) -> list[ScriptCron]:
    engine = create_engine(f"sqlite:///{tmp_path / 'korpha.db'}")
    with Session(engine) as s:
        return list(s.exec(select(ScriptCron)).all())


def test_add_vacuum_creates_cron_with_default_settings(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["cron", "add-vacuum"])
    assert result.exit_code == 0, result.stdout
    assert "Disk-vacuum cron added" in result.stdout
    jobs = _read_jobs(tmp)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "disk-vacuum"
    assert job.cadence == "every 7d"
    assert job.deliver_platform is None
    # Script written
    body = Path(job.script_path).read_text()
    assert "korpha" in body
    assert "disk vacuum" in body
    # silent-on-clean watchdog pattern
    assert "grep" in body


def test_add_vacuum_with_skip_db(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-vacuum", "--skip-db",
    ])
    assert result.exit_code == 0, result.stdout
    job = _read_jobs(tmp)[0]
    body = Path(job.script_path).read_text()
    assert "--skip-db" in body


def test_add_vacuum_with_custom_cadence(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-vacuum", "--every", "every 24h",
    ])
    assert result.exit_code == 0, result.stdout
    job = _read_jobs(tmp)[0]
    assert job.cadence == "every 24h"


def test_add_vacuum_custom_name(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-vacuum", "--name", "weekly-cleanup",
    ])
    assert result.exit_code == 0, result.stdout
    job = _read_jobs(tmp)[0]
    assert job.name == "weekly-cleanup"


def test_add_vacuum_rejects_duplicate_name(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["cron", "add-vacuum"])
    result = cli_runner.invoke(app, ["cron", "add-vacuum"])
    assert result.exit_code == 1
    assert "already exists" in result.stdout


def test_add_vacuum_rejects_bad_cadence(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-vacuum", "--every", "soon",
    ])
    assert result.exit_code == 1


def test_add_vacuum_rejects_bad_name(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-vacuum", "--name", "../escape",
    ])
    assert result.exit_code == 1
    assert "invalid" in result.stdout
