"""Tests for `korpha kanban` CLI commands."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from typer.testing import CliRunner

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.kanban.model import (
    KanbanCard, KanbanColumn,  # noqa: F401  -- ensure model registered
)


@pytest.fixture
def runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, Path]:
    """Pin KORPHA_DATA_DIR + DB; seed a founder + business."""
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


def _read_cards(tmp_path: Path) -> list[KanbanCard]:
    engine = create_engine(f"sqlite:///{tmp_path / 'korpha.db'}")
    with Session(engine) as s:
        return list(s.exec(select(KanbanCard)).all())


# ---- list ----


def test_kanban_list_empty(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["kanban", "list"])
    assert result.exit_code == 0, result.stdout
    assert "Board is empty" in result.stdout


def test_kanban_list_shows_added_card(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["kanban", "add", "Launch landing page"])
    result = cli_runner.invoke(app, ["kanban", "list"])
    assert result.exit_code == 0, result.stdout
    assert "BACKLOG" in result.stdout
    assert "Launch landing page" in result.stdout


def test_kanban_list_filter_to_one_column(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["kanban", "add", "x"])
    result = cli_runner.invoke(app, ["kanban", "list", "-c", "ready"])
    assert result.exit_code == 0
    assert "ready: empty" in result.stdout.lower()


def test_kanban_list_unknown_column(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "kanban", "list", "-c", "nonsense",
    ])
    assert result.exit_code == 1
    assert "Unknown column" in result.stdout


# ---- add ----


def test_kanban_add_creates_card(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(
        app, ["kanban", "add", "Launch the demo video"],
    )
    assert result.exit_code == 0, result.stdout
    assert "Added to BACKLOG" in result.stdout
    cards = _read_cards(tmp)
    assert len(cards) == 1
    assert cards[0].title == "Launch the demo video"
    assert cards[0].column == KanbanColumn.BACKLOG


def test_kanban_add_with_priority_and_owner(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "kanban", "add", "fix auth bug",
        "--priority", "high", "--owner", "cto",
        "--body", "users can't sign in via Google",
    ])
    assert result.exit_code == 0, result.stdout
    cards = _read_cards(tmp)
    assert cards[0].priority.value == "high"
    assert cards[0].owner_role == "cto"
    assert "Google" in cards[0].body


def test_kanban_add_blank_title_rejected(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["kanban", "add", "   "])
    assert result.exit_code == 1
    assert "Title required" in result.stdout


def test_kanban_add_bad_priority_rejected(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "kanban", "add", "x", "--priority", "URGENT",
    ])
    assert result.exit_code == 1
    assert "Priority must be" in result.stdout


def test_kanban_add_bad_owner_rejected(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "kanban", "add", "x", "--owner", "ninja",
    ])
    assert result.exit_code == 1
    assert "Owner must be" in result.stdout


# ---- move ----


def test_kanban_move_with_uuid_prefix(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["kanban", "add", "x"])
    cards = _read_cards(tmp)
    prefix = str(cards[0].id)[:8]
    result = cli_runner.invoke(app, [
        "kanban", "move", prefix, "specify",
    ])
    assert result.exit_code == 0, result.stdout
    assert "specify" in result.stdout

    cards_after = _read_cards(tmp)
    assert cards_after[0].column == KanbanColumn.SPECIFY


def test_kanban_move_invalid_transition(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["kanban", "add", "x"])
    cards = _read_cards(tmp)
    prefix = str(cards[0].id)[:8]
    result = cli_runner.invoke(app, [
        "kanban", "move", prefix, "done",
    ])
    assert result.exit_code == 1
    assert "cannot move" in result.stdout


def test_kanban_move_unknown_prefix(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "kanban", "move", "deadbeef", "specify",
    ])
    assert result.exit_code == 1
    assert "No card matches" in result.stdout


# ---- specify ----


def test_kanban_specify_attaches_criteria(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["kanban", "add", "x"])
    cards = _read_cards(tmp)
    prefix = str(cards[0].id)[:8]
    result = cli_runner.invoke(app, [
        "kanban", "specify", prefix,
        "-c", "page deployed",
        "-c", "Stripe button works",
        "--owner", "cto",
    ])
    assert result.exit_code == 0, result.stdout
    cards_after = _read_cards(tmp)
    assert len(cards_after[0].acceptance_criteria) == 2
    assert cards_after[0].owner_role == "cto"


def test_kanban_specify_requires_at_least_one_criterion(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["kanban", "add", "x"])
    cards = _read_cards(tmp)
    prefix = str(cards[0].id)[:8]
    result = cli_runner.invoke(app, [
        "kanban", "specify", prefix,
    ])
    # Typer auto-rejects missing required option with exit 2
    assert result.exit_code != 0


# ---- archive ----


def test_kanban_archive_moves_to_archived(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    cli_runner.invoke(app, ["kanban", "add", "x"])
    cards = _read_cards(tmp)
    prefix = str(cards[0].id)[:8]
    result = cli_runner.invoke(app, [
        "kanban", "archive", prefix,
    ])
    assert result.exit_code == 0, result.stdout
    assert "Archived" in result.stdout
    cards_after = _read_cards(tmp)
    assert cards_after[0].column == KanbanColumn.ARCHIVED
