"""Tests for the insights engine + dashboard panel.

Cover:
  - Empty window (no rows) returns zeros without crashing
  - Cost rollup across multiple Cost rows
  - Provider/model/tier breakdown sorted by cost
  - Skills counted from Activity rows (skill.completed > skill.invoked)
  - Active days = count of distinct date-buckets across Cost + Activity
  - Hours-saved heuristic edges (zero, override env var)
  - Headline string contains the right pieces for marketing screenshots
  - Dashboard route renders for both empty + populated windows
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine

from korpha.audit.model import Activity, ActorType, Cost, InferenceTier
from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.insights import (
    InsightsReport,
    compute_insights,
    estimate_hours_saved,
    render_insights_terminal,
)


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path}/insights.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_business(session: Session) -> tuple[Founder, Business]:
    founder = Founder(email="x@y.com", display_name="X")
    session.add(founder)
    session.commit()
    session.refresh(founder)
    business = Business(name="B", description="d", founder_id=founder.id)
    session.add(business)
    session.commit()
    session.refresh(business)
    return founder, business


# ---- estimate_hours_saved ----


def test_estimate_zero_when_no_activity() -> None:
    assert estimate_hours_saved(0, 0) == 0.0


def test_estimate_uses_default_minutes_per_skill() -> None:
    """10 skill calls × 6 minutes = 60 minutes = 1.0h."""
    out = estimate_hours_saved(10, 10, minutes_per_skill=6.0)
    assert out == pytest.approx(1.0)


def test_estimate_chat_only_calls_count_at_third_rate() -> None:
    """20 inference calls + 0 skills → 20 * (6/3) min = 40 min."""
    out = estimate_hours_saved(0, 20, minutes_per_skill=6.0)
    assert out == pytest.approx(40 / 60.0)


def test_estimate_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KORPHA_INSIGHTS_MIN_PER_SKILL", "10")
    # Without explicit override → should use env var
    out = estimate_hours_saved(6, 6)
    assert out == pytest.approx(1.0)  # 6 calls × 10 min = 60 min


def test_estimate_invalid_env_var_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_INSIGHTS_MIN_PER_SKILL", "garbage")
    out = estimate_hours_saved(10, 10)
    # Default 6 min/skill → 1.0h
    assert out == pytest.approx(1.0)


# ---- compute_insights: empty ----


def test_compute_empty_window_returns_zeros(session: Session) -> None:
    _, business = _seed_business(session)
    report = compute_insights(session, business_id=business.id, window_days=7)
    assert report.total_cost_usd == 0.0
    assert report.inference_calls == 0
    assert report.skills_run == 0
    assert report.active_days == 0
    assert report.estimated_hours_saved == 0.0
    assert report.by_provider == ()
    assert report.top_skills == ()


def test_empty_report_headline_is_friendly(session: Session) -> None:
    _, business = _seed_business(session)
    report = compute_insights(session, business_id=business.id, window_days=7)
    headline = report.headline()
    assert "0 skills" in headline
    assert "last 7d" in headline


# ---- compute_insights: populated ----


def _add_cost(
    session: Session,
    business_id,
    *,
    provider: str,
    model: str,
    tier: InferenceTier = InferenceTier.PRO,
    cost: float = 0.01,
    in_tok: int = 1000,
    out_tok: int = 500,
    cached: int = 0,
    when: datetime | None = None,
) -> Cost:
    row = Cost(
        business_id=business_id,
        provider=provider,
        model=model,
        tier=tier,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cached_tokens=cached,
        cost_usd=Decimal(str(cost)),
        created_at=when,
    )
    session.add(row)
    session.commit()
    return row


def _add_activity(
    session: Session,
    business_id,
    *,
    event_type: str,
    skill_name: str = "",
    role: str = "",
    when: datetime | None = None,
) -> Activity:
    row = Activity(
        business_id=business_id,
        actor_type=ActorType.AGENT,
        event_type=event_type,
        payload={
            "skill_name": skill_name, "role": role,
        } if skill_name else {},
        created_at=when,
    )
    session.add(row)
    session.commit()
    return row


def test_compute_aggregates_cost_across_rows(session: Session) -> None:
    _, business = _seed_business(session)
    _add_cost(session, business.id, provider="deepseek", model="m1", cost=0.10)
    _add_cost(session, business.id, provider="deepseek", model="m1", cost=0.05)
    _add_cost(session, business.id, provider="kimi", model="k1", cost=0.20)

    report = compute_insights(
        session, business_id=business.id, window_days=30,
    )
    assert report.total_cost_usd == pytest.approx(0.35)
    assert report.inference_calls == 3
    # Tokens summed too
    assert report.total_input_tokens == 3000
    assert report.total_output_tokens == 1500


def test_compute_provider_breakdown_sorted_by_cost(session: Session) -> None:
    _, business = _seed_business(session)
    _add_cost(session, business.id, provider="cheap", model="m", cost=0.02)
    _add_cost(session, business.id, provider="cheap", model="m", cost=0.02)
    _add_cost(session, business.id, provider="expensive", model="m", cost=0.99)

    report = compute_insights(session, business_id=business.id, window_days=30)
    assert len(report.by_provider) == 2
    # Most expensive first
    assert report.by_provider[0].provider == "expensive"
    assert report.by_provider[1].provider == "cheap"
    assert report.by_provider[1].calls == 2


def test_compute_skill_counts_from_completed_events(session: Session) -> None:
    _, business = _seed_business(session)
    _add_activity(
        session, business.id,
        event_type="skill.completed",
        skill_name="niche.find_micro_niches",
        role="ceo",
    )
    _add_activity(
        session, business.id,
        event_type="skill.completed",
        skill_name="niche.find_micro_niches",
        role="ceo",
    )
    _add_activity(
        session, business.id,
        event_type="skill.completed",
        skill_name="landing.draft_copy",
        role="cmo",
    )
    # Invoked-without-completed should be ignored when completed exists
    _add_activity(
        session, business.id,
        event_type="skill.invoked",
        skill_name="research.scrape",
        role="cto",
    )

    report = compute_insights(session, business_id=business.id, window_days=30)
    assert report.skills_run == 3
    names = [s.skill_name for s in report.top_skills]
    assert names[0] == "niche.find_micro_niches"
    assert report.top_skills[0].calls == 2


def test_compute_falls_back_to_invoked_when_no_completed(
    session: Session,
) -> None:
    """Older Activity rows may only have skill.invoked. Don't drop
    them silently — count them when no completed events exist."""
    _, business = _seed_business(session)
    _add_activity(
        session, business.id,
        event_type="skill.invoked",
        skill_name="x",
    )
    _add_activity(
        session, business.id,
        event_type="skill.invoked",
        skill_name="x",
    )
    report = compute_insights(session, business_id=business.id, window_days=30)
    assert report.skills_run == 2


def test_compute_active_days_counts_distinct_dates(session: Session) -> None:
    _, business = _seed_business(session)
    base = datetime.now(tz=timezone.utc)
    # Same day, three calls
    for _ in range(3):
        _add_cost(session, business.id, provider="p", model="m", when=base)
    # Different day
    _add_cost(
        session, business.id, provider="p", model="m",
        when=base - timedelta(days=2),
    )
    report = compute_insights(session, business_id=business.id, window_days=30)
    assert report.active_days == 2


def test_compute_window_excludes_old_rows(session: Session) -> None:
    """Cost row outside the window must not contribute to totals."""
    _, business = _seed_business(session)
    base = datetime.now(tz=timezone.utc)
    _add_cost(
        session, business.id, provider="p", model="m",
        cost=999.0, when=base - timedelta(days=60),
    )
    _add_cost(
        session, business.id, provider="p", model="m",
        cost=1.0, when=base,
    )
    report = compute_insights(session, business_id=business.id, window_days=7)
    assert report.total_cost_usd == pytest.approx(1.0)
    assert report.inference_calls == 1


def test_compute_other_business_data_isolated(session: Session) -> None:
    """Two businesses share the DB; insights for A must not include
    B's costs. Multi-tenant safety."""
    founder = Founder(email="a@y.com", display_name="A")
    session.add(founder)
    session.commit()
    session.refresh(founder)
    biz_a = Business(name="A", founder_id=founder.id, description="")
    biz_b = Business(name="B", founder_id=founder.id, description="")
    session.add_all([biz_a, biz_b])
    session.commit()
    session.refresh(biz_a)
    session.refresh(biz_b)
    _add_cost(session, biz_b.id, provider="p", model="m", cost=99.0)
    _add_cost(session, biz_a.id, provider="p", model="m", cost=1.0)
    report = compute_insights(session, business_id=biz_a.id, window_days=30)
    assert report.total_cost_usd == pytest.approx(1.0)
    assert report.inference_calls == 1


# ---- headline ----


def test_headline_contains_money_shot_pieces(session: Session) -> None:
    _, business = _seed_business(session)
    _add_cost(session, business.id, provider="p", model="m", cost=14.32)
    for _ in range(132):
        _add_activity(
            session, business.id,
            event_type="skill.completed",
            skill_name="x", role="ceo",
        )
    report = compute_insights(session, business_id=business.id, window_days=30)
    headline = report.headline()
    assert "$14.32" in headline
    assert "132 skills" in headline
    # Hours saved: 132 skills × 6 min = 792 min ≈ 13.2h
    assert "13.2h" in headline


def test_headline_uses_minutes_when_under_one_hour(session: Session) -> None:
    _, business = _seed_business(session)
    # 5 skills × 6 min = 30 min < 1h — should render as minutes
    for _ in range(5):
        _add_activity(
            session, business.id,
            event_type="skill.completed",
            skill_name="x", role="ceo",
        )
    report = compute_insights(session, business_id=business.id, window_days=7)
    headline = report.headline()
    assert "m" in headline.split("saved you ~")[1].split(" ")[0]


def test_headline_uses_4dp_for_micro_costs(session: Session) -> None:
    """Open-weights provider = ~$0.0001/call. Show 4dp so the
    number isn't '$0.00'."""
    _, business = _seed_business(session)
    _add_cost(session, business.id, provider="p", model="m", cost=0.0042)
    report = compute_insights(session, business_id=business.id, window_days=7)
    assert "$0.0042" in report.headline()


# ---- terminal renderer ----


def test_render_terminal_no_color_clean(session: Session) -> None:
    _, business = _seed_business(session)
    _add_cost(session, business.id, provider="p", model="m", cost=0.10)
    _add_activity(
        session, business.id,
        event_type="skill.completed",
        skill_name="niche.find", role="ceo",
    )
    report = compute_insights(session, business_id=business.id, window_days=7)
    out = render_insights_terminal(report, color=False)
    assert "\x1b[" not in out
    assert "Spend by provider" in out
    assert "Top skills" in out
    assert "niche.find" in out


def test_render_terminal_empty_shows_friendly_message(
    session: Session,
) -> None:
    _, business = _seed_business(session)
    report = compute_insights(session, business_id=business.id, window_days=7)
    out = render_insights_terminal(report, color=False)
    assert "No activity" in out


# ---- dashboard route ----


def _seed_dashboard(data_dir: Path):
    """Mirror tests/test_skills_authored_dashboard.py::_seed_business —
    the dashboard ``_ctx`` helper requires a founder + business +
    a CEO AgentRole row to render any /app route. Returns the
    business UUID so the caller can hydrate fresh objects in their
    own session (avoids DetachedInstanceError)."""
    from korpha.cofounder.model import AgentRole, RoleType

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
        role = AgentRole(
            business_id=b.id, role_type=RoleType.CEO, title="CEO",
        )
        session.add(role)
        session.commit()
        return b.id


def test_dashboard_insights_route_renders_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end smoke: route exists, returns 200, mentions 'No activity'
    when no data."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    _seed_dashboard(tmp_path)

    from fastapi.testclient import TestClient

    from korpha.api.server import build_app

    client = TestClient(build_app())
    resp = client.get("/app/insights")
    assert resp.status_code == 200
    assert "No activity" in resp.text


def test_dashboard_insights_route_renders_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    business_id = _seed_dashboard(tmp_path)

    db_path = tmp_path / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        _add_cost(s, business_id, provider="kimi", model="k1", cost=0.50)
        for _ in range(8):
            _add_activity(
                s, business_id,
                event_type="skill.completed",
                skill_name="niche.find_micro_niches", role="ceo",
            )

    from fastapi.testclient import TestClient
    from korpha.api.server import build_app

    client = TestClient(build_app())
    resp = client.get("/app/insights?days=7")
    assert resp.status_code == 200
    text = resp.text
    assert "$0.5000" in text
    assert "niche.find_micro_niches" in text
    assert "8 skills" in text
    # Window picker shows the right active option
    assert "insights-window-active" in text


def test_dashboard_insights_clamps_extreme_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """?days=99999 must not query 274 years of nothing — clamp to 365."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    _seed_dashboard(tmp_path)

    from fastapi.testclient import TestClient
    from korpha.api.server import build_app

    client = TestClient(build_app())
    resp = client.get("/app/insights?days=99999")
    assert resp.status_code == 200
