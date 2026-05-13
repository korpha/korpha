"""PR12 tests — `korpha unit ...` CLI commands."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from korpha.cli import app


@pytest.fixture
def runner_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, Path]:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "KORPHA_DB_URL", f"sqlite:///{tmp_path / 'cli.db'}",
    )
    return CliRunner(), tmp_path


def test_unit_list_empty(
    runner_env: tuple[CliRunner, Path],
) -> None:
    runner, _ = runner_env
    result = runner.invoke(app, ["unit", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No units" in result.stdout


def test_unit_help(
    runner_env: tuple[CliRunner, Path],
) -> None:
    runner, _ = runner_env
    result = runner.invoke(app, ["unit", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "show" in result.stdout
    assert "backup" in result.stdout
    assert "pause" in result.stdout
    assert "archive" in result.stdout


def test_unit_show_missing_id(
    runner_env: tuple[CliRunner, Path],
) -> None:
    runner, _ = runner_env
    from uuid import uuid4
    result = runner.invoke(app, ["unit", "show", str(uuid4())])
    # Error goes to stderr; check exit code is what matters
    assert result.exit_code == 1


def test_unit_list_after_seed(
    runner_env: tuple[CliRunner, Path],
) -> None:
    """Spin up a unit via direct DB access, then list via CLI."""
    runner, tmp = runner_env
    from sqlmodel import Session, SQLModel, create_engine
    import korpha.db.registry  # noqa: F401
    from korpha.business.model import Business
    from korpha.business_units.board import BusinessUnitBoard
    from korpha.business_units.model import BusinessUnitKind
    from korpha.identity.model import Founder

    db_url = os.environ["KORPHA_DB_URL"]
    engine = create_engine(db_url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        f = Founder(email="a@b.com", display_name="A")
        session.add(f); session.commit(); session.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="x", founder_brief={},
        )
        session.add(b); session.commit(); session.refresh(b)
        BusinessUnitBoard(session).create(
            business_id=b.id, name="KDP",
            kind=BusinessUnitKind.DEFAULT,
        )

    result = runner.invoke(app, ["unit", "list"])
    assert result.exit_code == 0, result.stdout
    assert "KDP" in result.stdout


def test_unit_backup_creates_archive(
    runner_env: tuple[CliRunner, Path],
) -> None:
    runner, tmp = runner_env
    from sqlmodel import Session, SQLModel, create_engine
    import korpha.db.registry  # noqa: F401
    from korpha.business.model import Business
    from korpha.business_units.board import BusinessUnitBoard
    from korpha.business_units.filesystem import (
        ensure_unit_layout,
    )
    from korpha.business_units.model import BusinessUnitKind
    from korpha.identity.model import Founder

    db_url = os.environ["KORPHA_DB_URL"]
    engine = create_engine(db_url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        f = Founder(email="a@b.com", display_name="A")
        session.add(f); session.commit(); session.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="x", founder_brief={},
        )
        session.add(b); session.commit(); session.refresh(b)
        unit = BusinessUnitBoard(session).create(
            business_id=b.id, name="KDP",
            kind=BusinessUnitKind.DEFAULT,
        )
        ensure_unit_layout(unit.id)
        uid = str(unit.id)

    result = runner.invoke(app, ["unit", "backup", uid])
    assert result.exit_code == 0, result.stdout
    assert "Wrote backup" in result.stdout


def test_unit_pause_resume_roundtrip(
    runner_env: tuple[CliRunner, Path],
) -> None:
    runner, _ = runner_env
    from sqlmodel import Session, SQLModel, create_engine
    import korpha.db.registry  # noqa: F401
    from korpha.business.model import Business
    from korpha.business_units.board import BusinessUnitBoard
    from korpha.business_units.model import BusinessUnitKind
    from korpha.identity.model import Founder

    db_url = os.environ["KORPHA_DB_URL"]
    engine = create_engine(db_url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        f = Founder(email="a@b.com", display_name="A")
        session.add(f); session.commit(); session.refresh(f)
        b = Business(
            founder_id=f.id, name="x", description="x", founder_brief={},
        )
        session.add(b); session.commit(); session.refresh(b)
        unit = BusinessUnitBoard(session).create(
            business_id=b.id, name="t",
            kind=BusinessUnitKind.DEFAULT,
        )
        uid = str(unit.id)

    r = runner.invoke(app, ["unit", "pause", uid, "--reason", "test"])
    assert r.exit_code == 0
    assert "Paused" in r.stdout

    r = runner.invoke(app, ["unit", "resume", uid])
    assert r.exit_code == 0
    assert "Resumed" in r.stdout
