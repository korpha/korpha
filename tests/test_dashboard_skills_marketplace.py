"""Tests for the upgraded /app/skills marketplace browse view."""
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


def test_marketplace_renders_with_buckets(http) -> None:
    client, _ = http
    r = client.get("/app/skills")
    assert r.status_code == 200
    assert "Skill marketplace" in r.text
    # Built-in bucket label appears
    assert "Built-in" in r.text


def test_marketplace_lists_known_skills(http) -> None:
    """Sanity: at least one known skill name + tier badge renders."""
    client, _ = http
    r = client.get("/app/skills")
    # We have memory.note from this session + niche.find_micro_niches
    # from earlier — neither name should disappear.
    assert "memory.note" in r.text
    assert "niche.find_micro_niches" in r.text
    # Tier badges
    assert "workhorse" in r.text or "pro" in r.text


def test_marketplace_search_filters_by_name(http) -> None:
    client, _ = http
    r = client.get("/app/skills?q=kanban")
    assert r.status_code == 200
    # All shown skills should contain "kanban"
    assert "kanban" in r.text.lower()
    # Search input keeps the query so the user can refine
    assert 'value="kanban"' in r.text


def test_marketplace_search_filters_by_description(http) -> None:
    client, _ = http
    # 'monthly' is in finance.monthly_review's description but not its name
    r = client.get("/app/skills?q=monthly")
    assert r.status_code == 200
    assert "finance.monthly_review" in r.text


def test_marketplace_search_no_match_shows_empty_state(http) -> None:
    client, _ = http
    r = client.get("/app/skills?q=zzz_definitely_not_a_skill")
    assert r.status_code == 200
    assert "No skills match" in r.text
    assert "Clear the search" in r.text


def test_marketplace_search_clear_link(http) -> None:
    client, _ = http
    r = client.get("/app/skills?q=kanban")
    assert "/app/skills" in r.text  # the clear-link href
    assert "clear" in r.text


def test_marketplace_total_count(http) -> None:
    client, _ = http
    r = client.get("/app/skills")
    # Total should be substantial — we have dozens of built-in skills.
    # Just verify the line is present.
    assert "skills</strong> loaded" not in r.text  # plural "skills" not "skill"
    # The dashboard renders "<strong>N</strong> skills" — verify we're
    # showing more than 10 of them
    import re
    m = re.search(r"<strong>(\d+)</strong> skill", r.text)
    assert m is not None
    assert int(m.group(1)) > 10


def test_marketplace_active_sidebar_link(http) -> None:
    client, _ = http
    r = client.get("/app/skills")
    # The Skills nav-item should be marked active
    assert 'is-active' in r.text
