"""Tests for the /app/team dashboard panel."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder


def _seed(data_dir: Path) -> None:
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
        HiringService(s).ensure_ceo(b.id)


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    _seed(tmp_path)
    from korpha.api.server import build_app
    return TestClient(build_app()), tmp_path


def test_team_page_renders_c_suite(http) -> None:
    client, _ = http
    r = client.get("/app/team")
    assert r.status_code == 200
    assert "C-suite" in r.text
    assert "CEO" in r.text


def test_team_page_empty_workers_state(http) -> None:
    client, _ = http
    r = client.get("/app/team")
    assert "No specialty workers hired" in r.text


def test_team_hire_creates_worker(http) -> None:
    client, tmp = http
    r = client.post(
        "/app/team/hire",
        data={"specialty": "copywriter"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "hired=copywriter" in r.headers["location"]

    r = client.get("/app/team")
    assert "copywriter" in r.text
    assert "Copywriter" in r.text


def test_team_hire_rejects_blank(http) -> None:
    client, _ = http
    r = client.post(
        "/app/team/hire",
        data={"specialty": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_team_hire_rejects_spaces(http) -> None:
    client, _ = http
    r = client.post(
        "/app/team/hire",
        data={"specialty": "copy writer"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_team_fire_drops_worker(http) -> None:
    client, tmp = http
    client.post(
        "/app/team/hire",
        data={"specialty": "copywriter"},
        follow_redirects=False,
    )
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        worker = s.exec(
            select(AgentRole)
            .where(AgentRole.role_type == RoleType.WORKER)
        ).one()
        wid = str(worker.id)

    r = client.post(
        f"/app/team/{wid}/fire", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "fired=" in r.headers["location"]


def test_team_fire_refuses_ceo(http) -> None:
    client, tmp = http
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        ceo = s.exec(
            select(AgentRole).where(AgentRole.role_type == RoleType.CEO)
        ).one()
    r = client.post(
        f"/app/team/{ceo.id}/fire", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=Refuses" in r.headers["location"]


def test_team_fire_unknown_id(http) -> None:
    from uuid import uuid4
    client, _ = http
    r = client.post(
        f"/app/team/{uuid4()}/fire", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Role+not+found" in r.headers["location"]


def test_team_fire_bad_uuid(http) -> None:
    client, _ = http
    r = client.post(
        "/app/team/not-a-uuid/fire", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Bad+role+id" in r.headers["location"]


def test_team_isolates_by_business(http, tmp_path: Path) -> None:
    """Workers from another business shouldn't show up here."""
    from uuid import uuid4
    client, tmp = http
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        other_biz = Business(
            founder_id=uuid4(), name="Other", description="",
        )
        s.add(other_biz); s.commit(); s.refresh(other_biz)
        HiringService(s).hire(
            other_biz.id, RoleType.WORKER,
            title="Theirs", specialty="x",
        )

    r = client.get("/app/team")
    assert "Theirs" not in r.text


def test_team_sidebar_link_present(http) -> None:
    client, _ = http
    r = client.get("/app/team")
    assert 'href="/app/team"' in r.text
