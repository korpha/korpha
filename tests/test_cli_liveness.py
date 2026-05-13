"""Tests for the `korpha liveness` CLI command."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from typer.testing import CliRunner

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.kanban import CreateCardInput, KanbanBoard
from korpha.kanban.model import KanbanCard, KanbanColumn


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


def test_liveness_clean_board(runner: tuple[CliRunner, Path]) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["liveness"])
    assert result.exit_code == 0, result.stdout
    assert "No stuck cards" in result.stdout


def test_liveness_flags_idle_card(
    runner: tuple[CliRunner, Path],
) -> None:
    cli_runner, tmp = runner
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        biz = s.exec(select(Business)).one()
        cto = AgentRole(
            business_id=biz.id, role_type=RoleType.CTO, title="CTO",
        )
        s.add(cto); s.commit(); s.refresh(cto)
        board = KanbanBoard(s)
        card = board.create(CreateCardInput(
            business_id=biz.id, title="wedged-task",
        ))
        board.specify(
            card.id, acceptance_criteria=["a"], owner_role="cto",
        )
        board.move(card.id, KanbanColumn.READY)
        board.claim(card.id, agent_role_id=cto.id, actor_role="cto")
        # Back-date moved_at past the idle threshold
        card = s.get(KanbanCard, card.id)
        card.moved_at = (
            datetime.now(tz=timezone.utc) - timedelta(hours=10)
        )
        s.add(card); s.commit()

    from korpha.cli import app
    result = cli_runner.invoke(app, ["liveness"])
    assert result.exit_code == 0
    assert "wedged-task" in result.stdout
    assert "idle in progress" in result.stdout


def test_liveness_no_business_fails_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    from korpha.cli import app
    result = CliRunner().invoke(app, ["liveness"])
    assert result.exit_code == 1
    assert "business" in result.stdout.lower()
