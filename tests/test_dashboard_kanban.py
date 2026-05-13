"""Tests for the dashboard kanban panel."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.kanban import CreateCardInput, KanbanBoard
from korpha.kanban.model import KanbanCard, KanbanColumn  # noqa: F401


def _seed(data_dir: Path) -> tuple[UUID, UUID]:
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="t", founder_brief={"goal": "t"},
        )
        s.add(b); s.commit(); s.refresh(b)
        s.add(AgentRole(
            business_id=b.id, role_type=RoleType.CEO, title="CEO",
        ))
        s.commit()
        return b.id, f.id


def _add_card(
    data_dir: Path, business_id: UUID, founder_id: UUID, *,
    title: str, body: str = "",
) -> UUID:
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        board = KanbanBoard(s)
        card = board.create(CreateCardInput(
            business_id=business_id,
            title=title, body=body,
            created_by_founder_id=founder_id,
        ))
        return card.id


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    business_id, founder_id = _seed(tmp_path)
    from korpha.api.server import build_app
    return TestClient(build_app()), business_id, founder_id, tmp_path


# ---- list ----


def test_kanban_page_renders_empty_columns(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/kanban")
    assert r.status_code == 200
    # all columns visible (except archived)
    for col in [
        "backlog", "specify", "ready", "in progress", "review", "done",
        "blocked",
    ]:
        assert col in r.text.lower()
    # quick-add form is present
    assert "Add to backlog" in r.text


def test_kanban_page_lists_cards(http) -> None:
    client, biz, founder, tmp = http
    _add_card(tmp, biz, founder, title="Launch landing page")
    _add_card(tmp, biz, founder, title="Write 3 LinkedIn posts")
    r = client.get("/app/kanban")
    assert "Launch landing page" in r.text
    assert "Write 3 LinkedIn posts" in r.text


def test_kanban_page_isolates_by_business(http) -> None:
    client, biz, founder, tmp = http
    _add_card(tmp, biz, founder, title="Ours card")
    # Other business's card must not appear
    other_biz = uuid4()
    other_founder = uuid4()
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        board = KanbanBoard(s)
        # bypass FK via direct insert
        from korpha.kanban.model import KanbanCard as _KC
        s.add(_KC(
            business_id=other_biz, title="Theirs card",
            created_by_founder_id=None,
        ))
        s.commit()
    r = client.get("/app/kanban")
    assert "Ours card" in r.text
    assert "Theirs card" not in r.text


# ---- new ----


def test_kanban_new_creates_backlog_card(http) -> None:
    client, biz, founder, tmp = http
    r = client.post(
        "/app/kanban/new",
        data={"title": "Test card from form"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/app/kanban?created=" in r.headers["location"]

    r = client.get("/app/kanban")
    assert "Test card from form" in r.text


def test_kanban_new_rejects_blank_title(http) -> None:
    client, _, _, _ = http
    r = client.post(
        "/app/kanban/new",
        data={"title": "   "},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=Title" in r.headers["location"]


# ---- move ----


def test_kanban_move_backlog_to_specify(http) -> None:
    client, biz, founder, tmp = http
    cid = _add_card(tmp, biz, founder, title="Move me")
    r = client.post(
        f"/app/kanban/{cid}/move",
        data={"to_column": "specify"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "moved=specify" in r.headers["location"]


def test_kanban_move_invalid_transition_surfaces_error(http) -> None:
    """BACKLOG → DONE is not allowed; the redirect ?error= shows
    the KanbanError message back to the founder."""
    client, biz, founder, tmp = http
    cid = _add_card(tmp, biz, founder, title="Skip me")
    r = client.post(
        f"/app/kanban/{cid}/move",
        data={"to_column": "done"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert "cannot+move" in r.headers["location"]


def test_kanban_move_unknown_card(http) -> None:
    client, _, _, _ = http
    r = client.post(
        f"/app/kanban/{uuid4()}/move",
        data={"to_column": "specify"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=Card+not+found" in r.headers["location"]


def test_kanban_move_bad_card_id(http) -> None:
    client, _, _, _ = http
    r = client.post(
        "/app/kanban/not-a-uuid/move",
        data={"to_column": "specify"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=Bad+card+id" in r.headers["location"]


def test_kanban_move_bad_target_column(http) -> None:
    client, biz, founder, tmp = http
    cid = _add_card(tmp, biz, founder, title="x")
    r = client.post(
        f"/app/kanban/{cid}/move",
        data={"to_column": "nonsense"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=Bad+target+column" in r.headers["location"]


def test_kanban_move_other_business_rejected(http) -> None:
    """Cross-business move attempts must be refused."""
    client, biz, founder, tmp = http
    other_biz = uuid4()
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        from korpha.kanban.model import KanbanCard as _KC
        c = _KC(business_id=other_biz, title="theirs")
        s.add(c); s.commit(); s.refresh(c)
        cid = c.id
    r = client.post(
        f"/app/kanban/{cid}/move",
        data={"to_column": "specify"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=Card+not+found" in r.headers["location"]


# ---- archive ----


# ---- live worker indicator ----


def test_kanban_marks_card_as_live_when_subagent_running(
    http, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a director attempt is running on the card's owner_role,
    the card renders with the pulse dot."""
    import asyncio

    client, biz, founder, tmp = http

    # Create a card claimed by a CTO
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        from korpha.cofounder.model import AgentRole, RoleType
        from korpha.kanban import CreateCardInput, KanbanBoard
        from korpha.kanban.model import KanbanColumn

        cto = AgentRole(
            business_id=biz, role_type=RoleType.CTO, title="CTO",
        )
        s.add(cto); s.commit(); s.refresh(cto)
        board = KanbanBoard(s)
        card = board.create(CreateCardInput(
            business_id=biz, title="deploy /pricing",
        ))
        board.specify(
            card.id, acceptance_criteria=["page up"], owner_role="cto",
        )
        board.move(card.id, KanbanColumn.READY)
        board.claim(card.id, agent_role_id=cto.id, actor_role="cto")

    # Inject a fake running subagent task into the registry
    from korpha.cofounder import workforce
    loop = asyncio.new_event_loop(); fake_task = loop.create_future()
    key = (str(biz), "cto")
    workforce._SUBAGENT_TASKS[key] = fake_task  # type: ignore[assignment]
    try:
        r = client.get("/app/kanban")
        assert r.status_code == 200
        # The card div carries kb-card-live, and the dot span renders.
        assert "kb-card kb-card-normal kb-card-live" in r.text
        assert '<span class="kb-live-dot"' in r.text
    finally:
        workforce._SUBAGENT_TASKS.pop(key, None)
        if not fake_task.done():
            fake_task.cancel()


def test_kanban_no_live_class_when_no_subagent_running(http) -> None:
    """Without an active subagent, IN_PROGRESS cards render plain
    (no pulse, no live class)."""
    client, biz, founder, tmp = http
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        from korpha.cofounder.model import AgentRole, RoleType
        from korpha.kanban import CreateCardInput, KanbanBoard
        from korpha.kanban.model import KanbanColumn

        cto = AgentRole(
            business_id=biz, role_type=RoleType.CTO, title="CTO",
        )
        s.add(cto); s.commit(); s.refresh(cto)
        board = KanbanBoard(s)
        card = board.create(CreateCardInput(
            business_id=biz, title="x",
        ))
        board.specify(
            card.id, acceptance_criteria=["a"], owner_role="cto",
        )
        board.move(card.id, KanbanColumn.READY)
        board.claim(card.id, agent_role_id=cto.id, actor_role="cto")

    r = client.get("/app/kanban")
    # The .kb-live-dot CSS rule is always in the stylesheet block;
    # check for the actual dot span markup which only renders when
    # a card is live.
    assert '<span class="kb-live-dot"' not in r.text


def test_kanban_live_indicator_isolates_by_business(
    http,
) -> None:
    """A live director on a different business doesn't light up
    cards in this business."""
    import asyncio
    from uuid import uuid4

    client, biz, _, tmp = http
    other_biz = str(uuid4())

    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        from korpha.cofounder.model import AgentRole, RoleType
        from korpha.kanban import CreateCardInput, KanbanBoard
        from korpha.kanban.model import KanbanColumn

        cto = AgentRole(
            business_id=biz, role_type=RoleType.CTO, title="CTO",
        )
        s.add(cto); s.commit(); s.refresh(cto)
        board = KanbanBoard(s)
        card = board.create(CreateCardInput(
            business_id=biz, title="x",
        ))
        board.specify(
            card.id, acceptance_criteria=["a"], owner_role="cto",
        )
        board.move(card.id, KanbanColumn.READY)
        board.claim(card.id, agent_role_id=cto.id, actor_role="cto")

    from korpha.cofounder import workforce
    loop = asyncio.new_event_loop(); fake_task = loop.create_future()
    # Foreign business, same role
    key = (other_biz, "cto")
    workforce._SUBAGENT_TASKS[key] = fake_task  # type: ignore[assignment]
    try:
        r = client.get("/app/kanban")
        # No live dot rendered for cards in another business
        assert '<span class="kb-live-dot"' not in r.text
    finally:
        workforce._SUBAGENT_TASKS.pop(key, None)
        if not fake_task.done():
            fake_task.cancel()


def test_kanban_archive_card_drops_it_from_view(http) -> None:
    client, biz, founder, tmp = http
    cid = _add_card(tmp, biz, founder, title="trash me")
    client.post(
        f"/app/kanban/{cid}/move",
        data={"to_column": "archived"},
        follow_redirects=False,
    )
    r = client.get("/app/kanban")
    assert "trash me" not in r.text
