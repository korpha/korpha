"""Tests for `korpha cron add-digest` preset."""
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
    """Pin KORPHA_DATA_DIR + DB to tmp_path; seed a founder + business."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    # Clear the cached engine so the new DB URL takes effect
    # inside the CLI invocation.
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo", description="t",
        )
        s.add(b); s.commit()
    from korpha.cli import app
    return CliRunner(), tmp_path


def _read_jobs(tmp_path: Path) -> list[ScriptCron]:
    engine = create_engine(f"sqlite:///{tmp_path / 'korpha.db'}")
    with Session(engine) as s:
        return list(s.exec(select(ScriptCron)).all())


def test_add_digest_creates_cron_with_default_settings(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(
        app, ["cron", "add-digest"],
    )
    assert result.exit_code == 0, result.stdout
    assert "Daily digest cron added" in result.stdout
    jobs = _read_jobs(tmp)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "daily-digest"
    assert job.cadence == "every 24h"
    assert job.deliver_platform is None
    # Script written
    assert Path(job.script_path).exists()
    body = Path(job.script_path).read_text()
    assert "korpha insights" in body
    assert "--days 1" in body


def test_add_digest_with_email_delivery(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-digest",
        "--every", "every 12h",
        "--deliver", "email",
        "--to", "mike@example.com",
        "--days", "7",
    ])
    assert result.exit_code == 0
    jobs = _read_jobs(tmp)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.cadence == "every 12h"
    assert job.deliver_platform == "email"
    assert job.deliver_recipient == "mike@example.com"
    body = Path(job.script_path).read_text()
    assert "--days 7" in body


def test_add_digest_custom_name(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-digest", "--name", "weekly-roi",
        "--every", "every 7d",
    ])
    assert result.exit_code == 0
    jobs = _read_jobs(tmp)
    assert len(jobs) == 1
    assert jobs[0].name == "weekly-roi"


def test_add_digest_rejects_bad_name(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-digest", "--name", "../escape",
    ])
    assert result.exit_code == 1
    assert "invalid" in result.stdout.lower()


def test_add_digest_rejects_bad_cadence(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-digest", "--every", "soon",
    ])
    assert result.exit_code == 1


def test_add_digest_requires_recipient_with_deliver(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-digest", "--deliver", "email",
    ])
    assert result.exit_code == 1
    assert "--to" in result.stdout


def test_add_digest_rejects_unknown_channel(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-digest", "--deliver", "fax", "--to", "555",
    ])
    assert result.exit_code == 1


def test_add_digest_rejects_negative_days(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-digest", "--days", "-1",
    ])
    assert result.exit_code == 1


def test_add_digest_rejects_duplicate_name(
    runner: tuple[CliRunner, Path],
) -> None:
    """Second invocation with the same --name fails — no silent
    overwrite of the cron + script."""
    cli_runner, _ = runner
    from korpha.cli import app
    first = cli_runner.invoke(app, ["cron", "add-digest"])
    assert first.exit_code == 0
    second = cli_runner.invoke(app, ["cron", "add-digest"])
    assert second.exit_code == 1
    assert "already exists" in second.stdout
