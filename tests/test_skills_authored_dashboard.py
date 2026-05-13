"""Tests for the authored-skills dashboard view.

Three routes under test:
  - GET  /app/skills/authored               — list view
  - GET  /app/skills/authored/{kind}/{slug}/source  — source preview
  - POST /app/skills/authored/{kind}/{slug}/delete  — remove from disk

Uses the existing test_api.py infrastructure (TestClient + temp data
dir + initialized founder/business). Path-traversal attempts get
their own targeted tests since this is the route that touches disk.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.api.server import build_app
from korpha.api.dashboard import (
    _enumerate_authored_skills,
    _find_authored_skill,
)
from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder


def _seed_business(data_dir: Path) -> None:
    """Make /app routes work — they require a founder + business."""
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        f = Founder(email="x@y.com", display_name="Mike")
        session.add(f)
        session.commit()
        session.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="test", founder_brief={"goal": "test"},
        )
        session.add(b)
        session.commit()
        session.refresh(b)
        # Route handlers require an AgentRole row to attach approvals to;
        # the dashboard reads roles indirectly. Add a CEO so /app/skills/*
        # doesn't redirect to onboarding.
        role = AgentRole(business_id=b.id, role_type=RoleType.CEO, title="CEO")
        session.add(role)
        session.commit()


def _write_yaml_skill(skills_dir: Path, slug: str, name: str) -> None:
    target = skills_dir / "agent_created" / slug
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.yaml").write_text(
        f"name: {name}\ndescription: test yaml skill\n",
        encoding="utf-8",
    )


def _write_python_skill(skills_dir: Path, slug: str, name: str) -> None:
    target = skills_dir / "agent_created" / "python" / slug
    target.mkdir(parents=True, exist_ok=True)
    (target / "skill.py").write_text(
        f"# stub source for {name}\n# (real skill would import + register)\n",
        encoding="utf-8",
    )
    (target / "manifest.yaml").write_text(
        f"name: {name}\ndescription: test python skill\n",
        encoding="utf-8",
    )


# ---- _enumerate_authored_skills helper ----


def test_enumerate_returns_empty_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    assert _enumerate_authored_skills() == []


def test_enumerate_finds_yaml_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    _write_yaml_skill(tmp_path, "support__triage", "support.triage")
    rows = _enumerate_authored_skills()
    assert len(rows) == 1
    assert rows[0]["kind"] == "yaml"
    assert rows[0]["slug"] == "support__triage"
    assert rows[0]["name"] == "support.triage"


def test_enumerate_finds_python_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    _write_python_skill(tmp_path, "teams__broadcast", "teams.broadcast")
    rows = _enumerate_authored_skills()
    assert len(rows) == 1
    assert rows[0]["kind"] == "python"
    assert rows[0]["name"] == "teams.broadcast"


def test_enumerate_combines_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    _write_yaml_skill(tmp_path, "yaml_one", "yaml.one")
    _write_python_skill(tmp_path, "py_one", "py.one")
    rows = _enumerate_authored_skills()
    kinds = {r["kind"] for r in rows}
    assert kinds == {"yaml", "python"}


def test_enumerate_skips_dirs_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory under agent_created/ without a manifest.yaml is
    not a skill — silently skip rather than raising."""
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    junk = tmp_path / "agent_created" / "leftover_dir"
    junk.mkdir(parents=True)
    (junk / "random.txt").write_text("hi", encoding="utf-8")
    assert _enumerate_authored_skills() == []


def test_enumerate_handles_malformed_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad YAML shouldn't crash the listing — falls back to slug-
    derived name + '(no description)'."""
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    target = tmp_path / "agent_created" / "broken__yaml"
    target.mkdir(parents=True)
    (target / "manifest.yaml").write_text("{bad: [unbalanced", encoding="utf-8")
    rows = _enumerate_authored_skills()
    assert len(rows) == 1
    # double-underscore in slug becomes dot in fallback name
    assert rows[0]["name"] == "broken.yaml"
    assert rows[0]["description"] == "(no description)"


# ---- _find_authored_skill (path traversal guards) ----


def test_find_rejects_path_traversal_dotdot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    assert _find_authored_skill("yaml", "../../etc/passwd") is None


def test_find_rejects_path_traversal_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    assert _find_authored_skill("yaml", "subdir/escape") is None


def test_find_rejects_unknown_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    _write_yaml_skill(tmp_path, "test_one", "test.one")
    assert _find_authored_skill("php", "test_one") is None


def test_find_resolves_valid_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    _write_yaml_skill(tmp_path, "test_one", "test.one")
    entry = _find_authored_skill("yaml", "test_one")
    assert entry is not None
    assert entry["name"] == "test.one"


# ---- HTTP routes ----


@pytest.fixture
def http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path / "skills"))
    _seed_business(tmp_path)
    return TestClient(build_app())


def test_authored_list_renders_empty(http_client: TestClient) -> None:
    r = http_client.get("/app/skills/authored")
    assert r.status_code == 200
    assert "No authored skills yet" in r.text


def test_authored_list_renders_skills(
    http_client: TestClient, tmp_path: Path,
) -> None:
    _write_yaml_skill(tmp_path / "skills", "y_one", "y.one")
    _write_python_skill(tmp_path / "skills", "p_one", "p.one")
    r = http_client.get("/app/skills/authored")
    assert r.status_code == 200
    assert "y.one" in r.text
    assert "p.one" in r.text
    # Source link + delete form should render
    assert "/source" in r.text
    assert "/delete" in r.text


def test_authored_source_renders(
    http_client: TestClient, tmp_path: Path,
) -> None:
    _write_yaml_skill(tmp_path / "skills", "src_test", "src.test")
    r = http_client.get("/app/skills/authored/yaml/src_test/source")
    assert r.status_code == 200
    # The actual file content should be in the page
    assert "test yaml skill" in r.text


def test_authored_source_redirects_for_unknown(
    http_client: TestClient,
) -> None:
    r = http_client.get(
        "/app/skills/authored/yaml/never_authored/source",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/skills/authored"


def test_authored_source_blocks_path_traversal(
    http_client: TestClient,
) -> None:
    """A slug containing ``..`` should never reach the file system —
    redirect to the list page rather than 500."""
    # FastAPI's path validator rejects '..' in path segments at the
    # routing layer; we get a 404 there. That's also fine — the key
    # is "no file content escapes."
    r = http_client.get(
        "/app/skills/authored/yaml/..%2Fetc%2Fpasswd/source",
        follow_redirects=False,
    )
    # Either 303 (handled by our slug check) or 404 (handled by router).
    assert r.status_code in (303, 404)


def test_authored_delete_removes_files(
    http_client: TestClient, tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_yaml_skill(skills_dir, "delete_me", "delete.me")
    target_dir = skills_dir / "agent_created" / "delete_me"
    assert target_dir.exists()

    r = http_client.post(
        "/app/skills/authored/yaml/delete_me/delete",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert not target_dir.exists()


def test_authored_delete_redirects_for_unknown(
    http_client: TestClient,
) -> None:
    r = http_client.post(
        "/app/skills/authored/yaml/never_existed/delete",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/skills/authored"


def test_authored_link_in_sidebar(http_client: TestClient) -> None:
    """Sidebar's "Authored" sub-link points at the new view. Without
    this the page is unreachable from the dashboard."""
    r = http_client.get("/app/dashboard")
    assert r.status_code in (200, 303)  # may redirect to onboard
    # Hit the chat page instead — it always renders + uses base.html
    r = http_client.get("/app/chat")
    assert r.status_code == 200
    assert "/app/skills/authored" in r.text
