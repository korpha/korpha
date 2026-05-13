"""Tests for `korpha disk` CLI commands."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from typer.testing import CliRunner

from korpha.business.model import Business
from korpha.identity.model import Founder


@pytest.fixture
def runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, Path]:
    """Pin everything under tmp_path so the disk report is
    self-contained and predictable."""
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


# ---- show ----


def test_disk_show_reports_db_size(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["disk"])
    assert result.exit_code == 0, result.stdout
    assert "Korpha disk usage" in result.stdout
    assert "Main DB (sqlite)" in result.stdout
    # absolute path printed
    assert str(tmp / "korpha.db") in result.stdout


def test_disk_show_reports_checkpoint_blobs(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    # Create a checkpoint so the blob store has content.
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "f.txt").write_text("hello")
    from korpha.checkpoints import snapshot
    snapshot(ws, label="t")

    from korpha.cli import app
    result = cli_runner.invoke(app, ["disk"])
    assert result.exit_code == 0
    assert "Checkpoint blobs" in result.stdout


def test_disk_show_includes_cron_scripts_dir(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    cron_dir = tmp / "cron-scripts"
    cron_dir.mkdir()
    (cron_dir / "watchdog.sh").write_text("#!/bin/sh\necho ok")

    from korpha.cli import app
    result = cli_runner.invoke(app, ["disk"])
    assert "Cron scripts" in result.stdout


def test_disk_show_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DB, no checkpoints, no anything."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path / "void"))
    monkeypatch.setenv(
        "KORPHA_DB_URL", f"sqlite:///{tmp_path / 'void' / 'x.db'}",
    )
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    from korpha.cli import app
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["disk"])
    assert result.exit_code == 0
    # Either "no Korpha data" OR a small DB section — both are OK.
    # Just check it didn't crash + ran the show path.


# ---- vacuum ----


def test_disk_vacuum_runs_clean(
    runner: tuple[CliRunner, Path],
) -> None:
    """Vacuum on a fresh store should report 0 reclaimed."""
    cli_runner, tmp = runner
    # Make a checkpoint so v2 has something to vacuum.
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "f.txt").write_text("hello")
    from korpha.checkpoints import snapshot
    snapshot(ws, label="t")

    from korpha.cli import app
    result = cli_runner.invoke(app, ["disk", "vacuum"])
    assert result.exit_code == 0, result.stdout
    assert "Vacuuming checkpoint blob store" in result.stdout
    assert "0 orphan blobs" in result.stdout
    assert "VACUUM" in result.stdout  # sqlite step ran


def test_disk_vacuum_reclaims_orphan_blob(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    # Drop an orphan blob in the v2 store
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "f.txt").write_text("hello")
    from korpha.checkpoints import snapshot
    snapshot(ws, label="t")

    from korpha.checkpoints.v2 import _blob_path
    orphan = _blob_path("e" * 64)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan content here")

    from korpha.cli import app
    result = cli_runner.invoke(app, ["disk", "vacuum"])
    assert result.exit_code == 0
    assert "1 orphan blobs removed" in result.stdout
    assert not orphan.exists()


def test_disk_vacuum_skip_db(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["disk", "vacuum", "--skip-db"])
    assert result.exit_code == 0
    assert "Skipping sqlite VACUUM" in result.stdout


def test_disk_vacuum_handles_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres URL → vacuum prints a "no-op for non-sqlite" note
    instead of crashing."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "KORPHA_DB_URL", "postgresql://u:p@localhost/db",
    )
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    from korpha.cli import app
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["disk", "vacuum"])
    assert result.exit_code == 0
    assert "Postgres" in result.stdout or "autovacuum" in result.stdout
