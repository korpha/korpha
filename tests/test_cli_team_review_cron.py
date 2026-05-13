"""Tests for the new CLI surfaces: korpha team, korpha review,
korpha cron add-monthly-review, korpha cron add-backup."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from typer.testing import CliRunner

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
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
        s.add(b); s.commit(); s.refresh(b)
        HiringService(s).ensure_ceo(b.id)
    return CliRunner(), tmp_path


# ---- korpha team list ----


def test_team_list_shows_ceo(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["team", "list"])
    assert result.exit_code == 0, result.stdout
    assert "C-suite" in result.stdout
    assert "CEO" in result.stdout


def test_team_list_includes_workers_after_hire(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, [
        "team", "hire", "copywriter", "--reason", "test",
    ])
    result = cli_runner.invoke(app, ["team", "list"])
    assert "Workers" in result.stdout
    assert "Copywriter" in result.stdout


def test_team_list_inactive_flag(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["team", "hire", "copywriter"])
    # Get the worker id and fire it
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        worker = s.exec(
            select(AgentRole)
            .where(AgentRole.role_type == RoleType.WORKER)
        ).one()
        worker_id = str(worker.id)
    cli_runner.invoke(app, [
        "team", "fire", worker_id[:8], "--reason", "test",
    ])
    # Default list excludes inactive
    result = cli_runner.invoke(app, ["team", "list"])
    assert "[fired]" not in result.stdout
    # --inactive includes them
    result = cli_runner.invoke(app, ["team", "list", "--inactive"])
    assert "[fired]" in result.stdout


# ---- korpha team hire ----


def test_team_hire_creates_worker(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "team", "hire", "copywriter",
    ])
    assert result.exit_code == 0, result.stdout
    assert "Hired" in result.stdout
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        workers = list(s.exec(
            select(AgentRole)
            .where(AgentRole.role_type == RoleType.WORKER)
        ).all())
    assert len(workers) == 1
    assert workers[0].specialty == "copywriter"


def test_team_hire_with_title(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, [
        "team", "hire", "support",
        "--title", "Customer Champion",
    ])
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        worker = s.exec(
            select(AgentRole)
            .where(AgentRole.role_type == RoleType.WORKER)
        ).one()
    assert worker.title == "Customer Champion"


def test_team_hire_rejects_specialty_with_space(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "team", "hire", "copy writer",
    ])
    assert result.exit_code == 1
    assert "Specialty" in result.stdout


# ---- korpha team fire ----


def test_team_fire_with_uuid_prefix(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["team", "hire", "copywriter"])
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        worker = s.exec(
            select(AgentRole)
            .where(AgentRole.role_type == RoleType.WORKER)
        ).one()
        prefix = str(worker.id)[:8]
    result = cli_runner.invoke(app, ["team", "fire", prefix])
    assert result.exit_code == 0
    assert "Fired" in result.stdout


def test_team_fire_refuses_ceo(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, tmp = runner
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        ceo = s.exec(
            select(AgentRole)
            .where(AgentRole.role_type == RoleType.CEO)
        ).one()
    from korpha.cli import app
    result = cli_runner.invoke(app, ["team", "fire", str(ceo.id)])
    assert result.exit_code == 1
    assert "Refuses" in result.stdout or "C-suite" in result.stdout


def test_team_fire_unknown_prefix(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["team", "fire", "deadbeef"])
    assert result.exit_code == 1
    assert "No worker" in result.stdout


# ---- korpha cron add-monthly-review ----


def test_cron_add_monthly_review_creates_cron(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-monthly-review",
    ])
    assert result.exit_code == 0, result.stdout
    assert "Monthly review cron added" in result.stdout

    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        crons = list(s.exec(select(ScriptCron)).all())
    assert len(crons) == 1
    assert crons[0].name == "monthly-review"
    assert crons[0].cadence == "every 30d"
    body = Path(crons[0].script_path).read_text()
    assert "korpha" in body
    assert "review" in body


def test_cron_add_monthly_review_deliver_email(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-monthly-review",
        "--deliver", "email", "--to", "mike@example.com",
    ])
    assert result.exit_code == 0
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        cron = s.exec(select(ScriptCron)).one()
    assert cron.deliver_platform == "email"
    assert cron.deliver_recipient == "mike@example.com"


def test_cron_add_monthly_review_rejects_dup_name(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["cron", "add-monthly-review"])
    result = cli_runner.invoke(app, ["cron", "add-monthly-review"])
    assert result.exit_code == 1
    assert "already exists" in result.stdout


# ---- korpha cron add-backup ----


def test_cron_add_backup_creates_cron_with_default_dir(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["cron", "add-backup"])
    assert result.exit_code == 0, result.stdout
    assert "Auto-backup cron added" in result.stdout

    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        cron = s.exec(select(ScriptCron)).one()
    assert cron.name == "auto-backup"
    assert cron.cadence == "every 7d"
    body = Path(cron.script_path).read_text()
    assert "backup" in body
    assert "tar.gz" in body


def test_cron_add_backup_custom_dir_and_keep(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    backup_dir = tmp / "external-backups"
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-backup",
        "--to", str(backup_dir),
        "--keep-last", "10",
    ])
    assert result.exit_code == 0
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        cron = s.exec(select(ScriptCron)).one()
    body = Path(cron.script_path).read_text()
    assert str(backup_dir) in body
    # keep-last math: tail -n +11 keeps top 10
    assert "tail -n +11" in body


def test_cron_add_backup_invalid_keep_last(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-backup", "--keep-last", "0",
    ])
    assert result.exit_code == 1
    assert "must be" in result.stdout


# ---- korpha review ----


def test_review_command_registered() -> None:
    """The review command exists in the app."""
    from korpha.cli import app
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["review", "--help"])
    assert result.exit_code == 0
    assert "monthly P&L" in result.stdout


def test_review_no_business_fails_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No business → review exits 1 with a clear message
    pointing at `korpha init`."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    # Init the schema but don't seed
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    from korpha.cli import app
    result = CliRunner().invoke(app, ["review"])
    assert result.exit_code == 1
    assert "business" in result.stdout.lower() or "onboard" in result.stdout.lower()
