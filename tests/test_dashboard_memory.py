"""Tests for the dashboard memory browser."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.memory.model import LongTermMemoryEntry  # noqa: F401


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


def _add_memory(
    data_dir: Path, business_id: UUID, founder_id: UUID,
    text: str, tags: list[str] | None = None,
) -> str:
    """Helper: write a memory row directly to the DB. Bypasses the
    skill so tests can pre-populate."""
    from uuid import uuid4 as _uuid4

    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        row = LongTermMemoryEntry(
            id=_uuid4(),
            business_id=business_id, founder_id=founder_id,
            text=text, tags=tags or [],
        )
        s.add(row); s.commit()
        return str(row.id)


@pytest.fixture
def http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    biz_id, founder_id = _seed(tmp_path)
    from korpha.api.server import build_app
    return TestClient(build_app()), biz_id, founder_id, tmp_path


# ---- list ----


def test_memory_page_empty_state(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/memory")
    assert r.status_code == 200
    assert "No stored memories yet" in r.text


def test_memory_page_lists_entries(http) -> None:
    client, biz_id, founder_id, tmp = http
    _add_memory(
        tmp, biz_id, founder_id,
        "Targeting freelance designers", tags=["niche"],
    )
    _add_memory(
        tmp, biz_id, founder_id,
        "Stripe configured for $29/month", tags=["billing"],
    )
    r = client.get("/app/memory")
    assert r.status_code == 200
    assert "Targeting freelance designers" in r.text
    assert "Stripe configured" in r.text
    assert "niche" in r.text
    assert "billing" in r.text
    # Forget button per row — count the actual button-class
    # attribute, not the CSS rule mentions
    assert r.text.count('class="memory-forget-btn"') == 2


def test_memory_page_search_filters(http) -> None:
    client, biz_id, founder_id, tmp = http
    _add_memory(
        tmp, biz_id, founder_id, "Targeting freelance designers",
    )
    _add_memory(
        tmp, biz_id, founder_id, "Stripe configured for $29/month",
    )
    r = client.get("/app/memory?q=stripe")
    assert r.status_code == 200
    assert "Stripe" in r.text
    assert "freelance designers" not in r.text


def test_memory_page_search_no_match(http) -> None:
    client, biz_id, founder_id, tmp = http
    _add_memory(tmp, biz_id, founder_id, "Targeting freelance designers")
    r = client.get("/app/memory?q=cryptocurrency")
    assert r.status_code == 200
    assert "No memories match" in r.text


def test_memory_page_isolates_by_business(http) -> None:
    """Multi-tenant: another business's memories don't leak."""
    client, biz_id, founder_id, tmp = http
    _add_memory(tmp, biz_id, founder_id, "ours")
    # Create a memory belonging to a different business
    _add_memory(tmp, uuid4(), founder_id, "theirs")
    r = client.get("/app/memory")
    assert "ours" in r.text
    assert "theirs" not in r.text


# ---- forget ----


def test_forget_post_redirects(http) -> None:
    client, biz_id, founder_id, tmp = http
    mid = _add_memory(tmp, biz_id, founder_id, "delete me")
    r = client.post(
        f"/app/memory/{mid}/forget", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "forgot=1" in r.headers["location"]
    # Subsequent GET no longer shows the row
    after = client.get("/app/memory")
    assert "delete me" not in after.text


def test_forget_unknown_id_returns_not_found_redirect(http) -> None:
    client, _, _, _ = http
    r = client.post(
        f"/app/memory/{uuid4()}/forget", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=not_found" in r.headers["location"]


def test_forget_bad_id_returns_bad_id_redirect(http) -> None:
    client, _, _, _ = http
    r = client.post(
        "/app/memory/not-a-uuid/forget", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=bad_id" in r.headers["location"]


def test_forget_other_business_memory_refused(http) -> None:
    """Multi-tenant: founder A can't forget founder B's memory
    even with the id."""
    client, biz_id, founder_id, tmp = http
    other_mid = _add_memory(
        tmp, uuid4(), founder_id, "not yours",
    )
    r = client.post(
        f"/app/memory/{other_mid}/forget", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=not_found" in r.headers["location"]


# ---- nav ----


def test_nav_includes_memory_link(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/memory")
    assert r.status_code == 200
    assert 'href="/app/memory"' in r.text
    assert 'is-active' in r.text
