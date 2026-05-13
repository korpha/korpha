"""Tests for the dashboard checkpoint browser + restore route."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.workspaces.model import Repo


def _seed(data_dir: Path) -> UUID:
    """Create a founder + business + CEO role + a Repo with a real
    on-disk path. Returns the Repo's UUID so tests can post to
    /app/checkpoints/{repo_id}/{snapshot_id}/restore."""
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    repo_path = data_dir / "fake-repo"
    repo_path.mkdir()
    (repo_path / "main.py").write_text("# original")
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
        repo = Repo(
            business_id=b.id, name="primary",
            local_path=str(repo_path),
        )
        s.add(repo); s.commit(); s.refresh(repo)
        return repo.id


@pytest.fixture
def http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, UUID, Path]:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    repo_id = _seed(tmp_path)
    from korpha.api.server import build_app
    return TestClient(build_app()), repo_id, tmp_path


# ---- list ----


def test_checkpoints_page_renders_empty_when_none(
    http: tuple[TestClient, UUID, Path],
) -> None:
    client, _, _ = http
    r = client.get("/app/checkpoints")
    assert r.status_code == 200
    assert "primary" in r.text  # repo name appears in section header
    assert "No checkpoints yet" in r.text


def test_checkpoints_page_lists_existing_snapshots(
    http: tuple[TestClient, UUID, Path],
) -> None:
    client, _repo_id, tmp_path = http
    from korpha.checkpoints import snapshot
    cp = snapshot(
        tmp_path / "fake-repo",
        label="before refactor",
    )
    r = client.get("/app/checkpoints")
    assert r.status_code == 200
    # The snapshot's id, label, and a Restore button should all appear
    assert cp.id in r.text
    assert "before refactor" in r.text
    assert "Restore" in r.text


def test_checkpoints_page_renders_empty_state_when_no_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A founder with a business but no Repo should see the
    helpful 'no repos to checkpoint yet' empty-state message,
    not a blank page."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
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

    from korpha.api.server import build_app
    client = TestClient(build_app())
    r = client.get("/app/checkpoints")
    assert r.status_code == 200
    assert "No repos to checkpoint yet" in r.text


# ---- restore ----


def test_restore_post_redirects_with_query_params(
    http: tuple[TestClient, UUID, Path],
) -> None:
    """A successful restore returns 303 → /app/checkpoints with
    ?restored=<id>&pre=<pre-id> so the page shows a flash."""
    client, repo_id, tmp_path = http
    from korpha.checkpoints import snapshot
    cp = snapshot(tmp_path / "fake-repo", label="initial")

    # Mutate, then restore
    (tmp_path / "fake-repo" / "main.py").write_text("# CHANGED")
    r = client.post(
        f"/app/checkpoints/{repo_id}/{cp.id}/restore",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/app/checkpoints" in r.headers["location"]
    assert f"restored={cp.id}" in r.headers["location"]
    assert "pre=" in r.headers["location"]
    # File restored
    assert (
        tmp_path / "fake-repo" / "main.py"
    ).read_text() == "# original"


def test_restore_post_returns_404_for_unknown_repo(
    http: tuple[TestClient, UUID, Path],
) -> None:
    """A POST to a repo_id that's not in this business's repos must
    not leak access — return 404."""
    client, _, _ = http
    bogus_repo = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        f"/app/checkpoints/{bogus_repo}/whatever/restore",
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_restore_post_returns_400_for_bad_repo_id(
    http: tuple[TestClient, UUID, Path],
) -> None:
    client, _, _ = http
    r = client.post(
        "/app/checkpoints/not-a-uuid/whatever/restore",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_restore_post_returns_500_for_unknown_snapshot(
    http: tuple[TestClient, UUID, Path],
) -> None:
    client, repo_id, _ = http
    r = client.post(
        f"/app/checkpoints/{repo_id}/deadbeef/restore",
        follow_redirects=False,
    )
    assert r.status_code == 500
    assert "Restore failed" in r.text


def test_restored_flash_renders_on_redirect_target(
    http: tuple[TestClient, UUID, Path],
) -> None:
    """After a successful restore, hitting the listing with the
    query params shows the success banner with the restore id."""
    client, repo_id, tmp_path = http
    from korpha.checkpoints import snapshot
    cp = snapshot(tmp_path / "fake-repo")
    r = client.get(
        f"/app/checkpoints?restored={cp.id}&pre=fakepre",
    )
    assert r.status_code == 200
    assert "Restored snapshot" in r.text
    assert cp.id in r.text
    assert "fakepre" in r.text


# ---- nav ----


def test_nav_includes_checkpoints_link(
    http: tuple[TestClient, UUID, Path],
) -> None:
    client, _, _ = http
    r = client.get("/app/checkpoints")
    assert r.status_code == 200
    # Sidebar link rendered
    assert 'href="/app/checkpoints"' in r.text
    # Active class applied on the current page
    assert 'is-active' in r.text
