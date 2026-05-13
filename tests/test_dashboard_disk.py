"""Tests for the /app/disk dashboard panel."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder


def _seed(data_dir: Path) -> None:
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit()
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="t", founder_brief={"goal": "t"},
        )
        s.add(b); s.commit(); s.refresh(b)
        s.add(AgentRole(
            business_id=b.id, role_type=RoleType.CEO, title="CEO",
        ))
        s.commit()


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


def test_disk_page_renders(http) -> None:
    client, _ = http
    r = client.get("/app/disk")
    assert r.status_code == 200
    assert "Korpha disk usage" not in r.text  # title suffix not duplicated
    assert "Total on disk" in r.text
    assert "Vacuum now" in r.text


def test_disk_page_shows_db_row(http) -> None:
    client, _ = http
    r = client.get("/app/disk")
    assert "Main DB (sqlite)" in r.text


def test_disk_page_shows_checkpoint_blobs_after_snapshot(http, tmp_path) -> None:
    client, tmp = http
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hi")
    from korpha.checkpoints import snapshot
    snapshot(ws, label="t")

    r = client.get("/app/disk")
    assert "Checkpoint blobs" in r.text


def test_disk_page_includes_sidebar_link(http) -> None:
    client, _ = http
    r = client.get("/app/disk")
    assert 'href="/app/disk"' in r.text
    assert "is-active" in r.text  # the disk link is the active one


def test_disk_vacuum_button_redirects_with_stats(http, tmp_path) -> None:
    client, tmp = http
    # Create a snapshot + drop an orphan blob so vacuum has work
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hi")
    from korpha.checkpoints import snapshot
    snapshot(ws, label="t")
    from korpha.checkpoints.v2 import _blob_path
    orphan = _blob_path("d" * 64)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    r = client.post("/app/disk/vacuum", follow_redirects=False)
    assert r.status_code == 303
    assert "/app/disk?vacuumed=1" in r.headers["location"]
    assert "blobs=1" in r.headers["location"]


def test_disk_vacuum_safe_when_nothing_to_clean(http) -> None:
    client, _ = http
    r = client.post("/app/disk/vacuum", follow_redirects=False)
    assert r.status_code == 303
    assert "vacuumed=1" in r.headers["location"]
    # blobs=0 expected on a clean fixture
    assert "blobs=0" in r.headers["location"]


def test_disk_page_renders_vacuumed_flash(http) -> None:
    client, _ = http
    r = client.get(
        "/app/disk?vacuumed=1&reclaimed=2.5+MB&blobs=3&tmp=1&db_reclaimed=120+KB",
    )
    assert "Vacuumed" in r.text
    assert "2.5 MB" in r.text
    assert "120 KB" in r.text
