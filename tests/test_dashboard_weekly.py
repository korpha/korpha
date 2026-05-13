"""Tests for the /app/weekly dashboard panel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.approvals.model import (
    ActionClass, Approval, ApprovalStatus,
)
from korpha.audit.model import Cost, InferenceTier
from korpha.blockers.model import (
    Blocker, BlockerKind, BlockerStatus, BlockerUrgency,
)
from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.kanban.model import KanbanCard, KanbanColumn


def _seed(data_dir: Path) -> tuple[str, str]:
    """Seed DB. Returns (business_id, ceo_role_id) as strings."""
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike Cofounder")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="t", founder_brief={"goal": "t"},
        )
        s.add(b); s.commit(); s.refresh(b)
        role = AgentRole(
            business_id=b.id, role_type=RoleType.CEO, title="CEO",
        )
        s.add(role); s.commit(); s.refresh(role)
        return str(b.id), str(role.id)


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    biz_id, role_id = _seed(tmp_path)
    from korpha.api.server import build_app
    return TestClient(build_app()), tmp_path, biz_id, role_id


def _add_card(
    tmp: Path, biz_id: str, *,
    title: str, column: KanbanColumn,
    days_ago_moved: int = 0, owner_role: str = "cto",
) -> None:
    from uuid import UUID
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    moved = datetime.now(tz=timezone.utc) - timedelta(days=days_ago_moved)
    with Session(engine) as s:
        c = KanbanCard(
            business_id=UUID(biz_id), title=title,
            column=column, owner_role=owner_role,
        )
        s.add(c); s.commit(); s.refresh(c)
        c.moved_at = moved
        s.add(c); s.commit()


def _add_cost(
    tmp: Path, biz_id: str, *,
    cost_usd: float, days_ago: int = 0,
    tier: InferenceTier = InferenceTier.WORKHORSE,
) -> None:
    from uuid import UUID
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    created = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    with Session(engine) as s:
        c = Cost(
            business_id=UUID(biz_id),
            provider="ollama-cloud", model="deepseek-v4-flash",
            tier=tier,
            input_tokens=100, output_tokens=200,
            cost_usd=Decimal(str(cost_usd)),
        )
        s.add(c); s.commit(); s.refresh(c)
        c.created_at = created
        s.add(c); s.commit()


def _add_pending_approval(tmp: Path, biz_id: str, role_id: str) -> None:
    from uuid import UUID
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        a = Approval(
            business_id=UUID(biz_id),
            agent_role_id=UUID(role_id),
            action_class=ActionClass.INTERNAL,
            proposal_summary="please approve",
        )
        s.add(a); s.commit()


def _add_blocker(
    tmp: Path, biz_id: str, role_id: str, *,
    title: str, urgency: BlockerUrgency = BlockerUrgency.HIGH,
) -> None:
    from uuid import UUID
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        b = Blocker(
            business_id=UUID(biz_id),
            requesting_agent_role_id=UUID(role_id),
            kind=BlockerKind.DECISION,
            urgency=urgency,
            status=BlockerStatus.OPEN,
            title=title,
        )
        s.add(b); s.commit()


# ---- happy path ----


def test_weekly_renders_for_empty_business(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/weekly")
    assert r.status_code == 200
    assert "This week" in r.text
    assert "Quiet week" in r.text


def test_weekly_shows_personalized_greeting(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/weekly")
    assert "Mike" in r.text  # display_name first word


def test_weekly_lists_shipped_cards(http) -> None:
    client, tmp, biz, _ = http
    _add_card(
        tmp, biz, title="ship landing",
        column=KanbanColumn.DONE, days_ago_moved=2,
    )
    _add_card(
        tmp, biz, title="post launch tweet",
        column=KanbanColumn.DONE, days_ago_moved=5, owner_role="cmo",
    )
    r = client.get("/app/weekly")
    assert "Shipped to DONE" in r.text
    assert "ship landing" in r.text
    assert "post launch tweet" in r.text
    assert "CTO" in r.text
    assert "CMO" in r.text


def test_weekly_excludes_old_done_cards(http) -> None:
    client, tmp, biz, _ = http
    # 10 days ago — outside the 7-day window
    _add_card(
        tmp, biz, title="ancient win",
        column=KanbanColumn.DONE, days_ago_moved=10,
    )
    r = client.get("/app/weekly")
    assert "ancient win" not in r.text
    # KPI shows 0 shipped this week
    assert ">0<" in r.text or ">0</div>" in r.text


def test_weekly_shows_in_progress_cards(http) -> None:
    client, tmp, biz, _ = http
    _add_card(
        tmp, biz, title="building docs",
        column=KanbanColumn.IN_PROGRESS, owner_role="cto",
    )
    r = client.get("/app/weekly")
    assert "Currently working on" in r.text
    assert "building docs" in r.text


def test_weekly_shows_review_cards_with_count(http) -> None:
    client, tmp, biz, _ = http
    _add_card(
        tmp, biz, title="check this", column=KanbanColumn.REVIEW,
    )
    _add_card(
        tmp, biz, title="and this", column=KanbanColumn.REVIEW,
    )
    r = client.get("/app/weekly")
    assert "Awaiting your review (2)" in r.text


def test_weekly_pending_approvals_kpi(http) -> None:
    client, tmp, biz, role_id = http
    _add_pending_approval(tmp, biz, role_id)
    _add_pending_approval(tmp, biz, role_id)
    r = client.get("/app/weekly")
    assert "awaiting your call" in r.text
    assert ">2<" in r.text


def test_weekly_lists_top_blockers(http) -> None:
    client, tmp, biz, role_id = http
    _add_blocker(tmp, biz, role_id, title="stripe key missing")
    r = client.get("/app/weekly")
    assert "Top blockers" in r.text
    assert "stripe key missing" in r.text


def test_weekly_spend_table_groups_by_tier(http) -> None:
    client, tmp, biz, _ = http
    _add_cost(tmp, biz, cost_usd=0.05, tier=InferenceTier.WORKHORSE)
    _add_cost(tmp, biz, cost_usd=0.10, tier=InferenceTier.WORKHORSE)
    _add_cost(tmp, biz, cost_usd=0.50, tier=InferenceTier.PRO)
    r = client.get("/app/weekly")
    assert "Where the spend went" in r.text
    assert "workhorse" in r.text.lower()
    assert "pro" in r.text.lower()
    assert "$0.65" in r.text  # total


def test_weekly_excludes_old_costs_from_spend(http) -> None:
    client, tmp, biz, _ = http
    _add_cost(tmp, biz, cost_usd=0.10, days_ago=2)
    _add_cost(tmp, biz, cost_usd=99.0, days_ago=30)  # outside window
    r = client.get("/app/weekly")
    assert "$0.10" in r.text
    assert "$99.00" not in r.text


def test_weekly_shipped_delta_first_week(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/weekly")
    assert "first week of activity" in r.text


def test_weekly_shipped_delta_with_prior_week(http) -> None:
    client, tmp, biz, _ = http
    _add_card(
        tmp, biz, title="this week",
        column=KanbanColumn.DONE, days_ago_moved=3,
    )
    _add_card(
        tmp, biz, title="last week",
        column=KanbanColumn.DONE, days_ago_moved=10,
    )
    _add_card(
        tmp, biz, title="last week 2",
        column=KanbanColumn.DONE, days_ago_moved=12,
    )
    r = client.get("/app/weekly")
    # 1 this week vs 2 last week → "-1 vs last week"
    assert "vs last week" in r.text


def test_weekly_isolates_by_business(http) -> None:
    """A different business's cards must not appear in this view."""
    from uuid import uuid4 as _uuid4
    client, tmp, _, _ = http
    other_biz = str(_uuid4())
    # Insert a foreign card directly
    engine = create_engine(f"sqlite:///{tmp / 'korpha.db'}")
    with Session(engine) as s:
        from uuid import UUID
        s.add(KanbanCard(
            business_id=UUID(other_biz), title="not yours",
            column=KanbanColumn.DONE,
        ))
        s.commit()
    r = client.get("/app/weekly")
    assert "not yours" not in r.text


def test_weekly_sidebar_link_present(http) -> None:
    client, _, _, _ = http
    r = client.get("/app/weekly")
    assert 'href="/app/weekly"' in r.text
