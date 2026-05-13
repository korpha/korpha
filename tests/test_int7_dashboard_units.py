"""PR-INT-7 tests — dashboard /app/units, /app/credentials, kanban
unit filter ribbon."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.kanban import CreateCardInput, KanbanBoard


def _seed(data_dir: Path) -> tuple[UUID, UUID]:
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="Marketro",
            description="t", founder_brief={"goal": "t"},
        )
        s.add(b); s.commit(); s.refresh(b)
        s.add(AgentRole(
            business_id=b.id, role_type=RoleType.CEO, title="CEO",
        ))
        # Seed a couple of BusinessUnits directly (skipping the skill
        # path so this test is hermetic).
        from korpha.business_units.board import BusinessUnitBoard
        board = BusinessUnitBoard(s)
        root = board.create(
            business_id=b.id, name="Marketro",
            kind=BusinessUnitKind.DEFAULT,
        )
        kdp = board.create(
            business_id=b.id, name="KDP",
            kind=BusinessUnitKind.LINE, parent_id=root.id,
        )
        # Add a card scoped to KDP + one company-wide for filter test
        kb = KanbanBoard(s)
        scoped = kb.create(CreateCardInput(
            business_id=b.id, title="KDP romance cover sweep",
            created_by_founder_id=f.id,
        ))
        # Set business_unit_id after create (CreateCardInput doesn't
        # expose it — direct write keeps this test minimal).
        scoped.business_unit_id = kdp.id
        s.add(scoped); s.commit()
        kb.create(CreateCardInput(
            business_id=b.id, title="Company-wide tax filing",
            created_by_founder_id=f.id,
        ))
        return b.id, kdp.id


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    biz_id, kdp_id = _seed(tmp_path)
    from korpha.api.server import build_app
    return TestClient(build_app()), biz_id, kdp_id, tmp_path


# ---------- /app/units ----------


def test_units_page_renders(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/units")
    assert r.status_code == 200
    assert "Business lines" in r.text
    # Root unit + KDP line should appear
    assert "Marketro" in r.text
    assert "KDP" in r.text
    # Status pill renders
    assert "tm-pill" in r.text


def test_units_page_includes_start_form_with_canonical_kinds(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/units")
    # The form's kind <select> needs the line kinds
    for k in ("pod", "kdp", "info", "saas"):
        assert k.upper() in r.text or k in r.text.lower()


# ---------- /app/credentials ----------


def test_credentials_page_renders_empty(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/credentials")
    assert r.status_code == 200
    assert "Credentials" in r.text
    # No OAuth CLIs configured yet
    assert "OAuth CLI pool" in r.text


def test_credentials_page_lists_oauth_clis(http) -> None:
    """When SharedResource(OAUTH_CLI) rows exist they show up."""
    client, biz, _, tmp = http
    db_path = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    from korpha.shared_resources.model import (
        SharedResource, SharedResourceKind,
    )
    with Session(engine) as s:
        s.add(SharedResource(
            business_id=biz, name="claude-code",
            label="Claude Code Pro", kind=SharedResourceKind.OAUTH_CLI,
            available_in_modes=["local"], config={},
            quota_limit_in_window=50, quota_window_seconds=18000,
        ))
        s.commit()

    r = client.get("/app/credentials")
    assert "claude-code" in r.text
    assert "Claude Code Pro" in r.text
    # Quota number shows
    assert "50" in r.text


# ---------- /app/kanban filter ribbon ----------


def test_kanban_filter_ribbon_renders(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/kanban")
    assert r.status_code == 200
    # Ribbon present with All + Company-wide + KDP pills
    assert "kb-filter-pill" in r.text
    assert "All" in r.text
    assert "Company-wide" in r.text
    assert "KDP" in r.text


def test_kanban_filter_by_unit(http) -> None:
    client, _biz, kdp_id, _ = http
    r = client.get(f"/app/kanban?unit={kdp_id}")
    assert r.status_code == 200
    assert "KDP romance cover sweep" in r.text
    # Company-wide card filtered out
    assert "Company-wide tax filing" not in r.text


def test_kanban_filter_company_wide(http) -> None:
    """?unit=__none__ shows only unscoped cards."""
    client, _, _, _ = http
    r = client.get("/app/kanban?unit=__none__")
    assert r.status_code == 200
    assert "Company-wide tax filing" in r.text
    assert "KDP romance cover sweep" not in r.text


def test_kanban_filter_invalid_uuid_falls_back_to_all(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/kanban?unit=not-a-uuid")
    assert r.status_code == 200
    # Both cards visible — bad filter degrades gracefully
    assert "KDP romance cover sweep" in r.text
    assert "Company-wide tax filing" in r.text
