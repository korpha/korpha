"""FastAPI server tests using TestClient.

Tests run against an in-memory SQLite (override KORPHA_DATA_DIR to a
tmp dir, then init the DB ourselves). LLM calls are short-circuited by
not setting OLLAMA_CLOUD_API_KEY — endpoints that need an LLM return 503.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import korpha.db.registry  # noqa: F401  -- registers all models
from korpha.api import build_app
from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.identity.model import Founder


@pytest.fixture
def temp_data_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        monkeypatch.setenv("KORPHA_DATA_DIR", str(path))
        # Ensure no LLM key leaks in from .env loaded earlier in the process.
        monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
        yield path


@pytest.fixture
def initialized_dir(temp_data_dir: Path) -> Path:
    """Data dir with founder + business + CEO already created."""
    db_path = temp_data_dir / "korpha.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        founder = Founder(email="mike@example.com", display_name="Mike")
        session.add(founder)
        session.commit()
        session.refresh(founder)
        business = Business(
            founder_id=founder.id,
            name="WidgetCo",
            description="Solo Python dev",
        )
        session.add(business)
        session.commit()
        session.refresh(business)
        HiringService(session).ensure_ceo(business.id)
    return temp_data_dir


def test_healthz(temp_data_dir: Path) -> None:
    client = TestClient(build_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["skills_loaded"], int)
    assert body["has_provider"] is False  # no key in test env


def test_me_404_when_no_business(temp_data_dir: Path) -> None:
    """No founder/business yet → /me returns 404."""
    db_path = temp_data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    client = TestClient(build_app())
    r = client.get("/me")
    assert r.status_code == 404


def test_me_returns_business(initialized_dir: Path) -> None:
    client = TestClient(build_app())
    r = client.get("/me")
    assert r.status_code == 200
    body = r.json()
    assert body["founder_email"] == "mike@example.com"
    assert body["founder_name"] == "Mike"
    assert body["business_name"] == "WidgetCo"


def test_pending_empty_list(initialized_dir: Path) -> None:
    client = TestClient(build_app())
    r = client.get("/approvals/pending")
    assert r.status_code == 200
    assert r.json() == []


def test_skills_list_includes_niche(temp_data_dir: Path) -> None:
    client = TestClient(build_app())
    r = client.get("/skills")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "niche.find_micro_niches" in names


def test_ask_503_when_no_provider(initialized_dir: Path) -> None:
    """No OLLAMA_CLOUD_API_KEY → /ask refuses with 503."""
    assert "OLLAMA_CLOUD_API_KEY" not in os.environ
    client = TestClient(build_app())
    r = client.post("/ask", json={"message": "hi"})
    assert r.status_code == 503


def test_skill_run_503_when_no_provider(initialized_dir: Path) -> None:
    client = TestClient(build_app())
    r = client.post(
        "/skills/niche.find_micro_niches/run",
        json={"args": {"skills": "Python"}},
    )
    assert r.status_code == 503


def test_skill_run_404_for_unknown_skill(temp_data_dir: Path) -> None:
    # Set a fake key so the provider check passes; the unknown-skill 404
    # comes from the registry, not the LLM provider.
    os.environ["OLLAMA_CLOUD_API_KEY"] = "fake-for-test"
    try:
        # Need an initialized DB for the founder/business lookup.
        db_path = temp_data_dir / "korpha.db"
        engine = create_engine(f"sqlite:///{db_path}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            founder = Founder(email="x@y.com", display_name="x")
            session.add(founder)
            session.commit()
            session.refresh(founder)
            biz = Business(founder_id=founder.id, name="x")
            session.add(biz)
            session.commit()
        client = TestClient(build_app())
        r = client.post("/skills/nonexistent.skill/run", json={"args": {}})
        assert r.status_code == 404
    finally:
        del os.environ["OLLAMA_CLOUD_API_KEY"]


def test_503_when_data_dir_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "KORPHA_DATA_DIR", "/nonexistent-path-for-korpha-test-12345/sub"
    )
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    client = TestClient(build_app())
    # /healthz still works (no DB needed).
    assert client.get("/healthz").status_code == 200
    # /me requires DB → 503.
    r = client.get("/me")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Dashboard onboard flow
# ---------------------------------------------------------------------------


def test_dashboard_redirects_to_onboard_when_brief_empty(
    initialized_dir: Path,
) -> None:
    """Fresh install: visiting /app/dashboard should bounce to /app/onboard
    so the Founder gets the Day-0 prompt instead of an empty dashboard."""
    client = TestClient(build_app(), follow_redirects=False)
    r = client.get("/app/dashboard")
    assert r.status_code == 303
    assert r.headers["location"] == "/app/onboard"


def test_dashboard_first_day_banner_visible_with_pending_chain_approvals(
    initialized_dir: Path,
) -> None:
    """When the chain has just produced approvals and the Founder hasn't
    acted on any, the dashboard surfaces a prominent banner pointing at
    the queue."""
    from korpha.approvals.model import ActionClass, Approval, ApprovalStatus
    from korpha.cofounder.hiring import HiringService

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        biz.founder_brief = {"goal": "$5k MRR", "summary": "x"}
        session.add(biz)
        ceo = HiringService(session).ensure_ceo(biz.id)
        for kind, action_class in [
            ("validation_report", ActionClass.INTERNAL),
            ("landing_copy", ActionClass.PUBLIC_POST),
            ("outreach_drafts", ActionClass.EMAIL_OUTREACH),
            ("create_payment_link", ActionClass.COMMERCE),
        ]:
            session.add(
                Approval(
                    business_id=biz.id, agent_role_id=ceo.id,
                    action_class=action_class,
                    proposal_summary=f"{kind} approval",
                    action_payload={"kind": kind, "result": {}},
                    status=ApprovalStatus.PENDING,
                )
            )
        session.commit()

    client = TestClient(build_app(), follow_redirects=False)
    r = client.get("/app/dashboard")
    assert r.status_code == 200
    body = r.text
    assert "Your cofounder shipped 4 drafts" in body
    assert "Stripe payment link" in body  # has_stripe rendered
    assert "Review queue →" in body


def test_dashboard_first_day_banner_hidden_when_all_actioned(
    initialized_dir: Path,
) -> None:
    """Once the Founder has approved/rejected the pending ones, the
    banner disappears — we don't want it nagging on day 30."""
    from korpha.approvals.model import ActionClass, Approval, ApprovalStatus
    from korpha.cofounder.hiring import HiringService

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        biz.founder_brief = {"goal": "$5k MRR", "summary": "x"}
        session.add(biz)
        ceo = HiringService(session).ensure_ceo(biz.id)
        # Approval on a chain kind, but already approved
        session.add(
            Approval(
                business_id=biz.id, agent_role_id=ceo.id,
                action_class=ActionClass.PUBLIC_POST,
                proposal_summary="lc",
                action_payload={"kind": "landing_copy", "result": {}},
                status=ApprovalStatus.APPROVED,
            )
        )
        session.commit()

    client = TestClient(build_app(), follow_redirects=False)
    r = client.get("/app/dashboard")
    assert r.status_code == 200
    assert "Your cofounder shipped" not in r.text


def test_dashboard_renders_when_brief_present(initialized_dir: Path) -> None:
    """With a captured brief, the dashboard renders normally."""
    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        biz.founder_brief = {"goal": "$5k MRR in 6 months", "summary": "x"}
        session.add(biz)
        session.commit()

    client = TestClient(build_app(), follow_redirects=False)
    r = client.get("/app/dashboard")
    assert r.status_code == 200
    assert "WidgetCo" in r.text


def test_onboard_form_renders(initialized_dir: Path) -> None:
    """GET /app/onboard always renders, regardless of brief state."""
    client = TestClient(build_app())
    r = client.get("/app/onboard")
    assert r.status_code == 200
    assert "Tell your cofounder about YOU" in r.text
    # The textarea should be empty on a fresh install.
    assert 'name="answer"' in r.text


def test_onboard_form_shows_existing_brief(initialized_dir: Path) -> None:
    """Returning Founder sees their previous summary so they know they're
    overwriting rather than appending."""
    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        biz.founder_brief = {
            "goal": "$5k MRR",
            "summary": "You want $5k MRR side income",
            "raw_answer": "i want 5k",
        }
        session.add(biz)
        session.commit()

    client = TestClient(build_app())
    r = client.get("/app/onboard")
    assert r.status_code == 200
    assert "Current brief" in r.text
    assert "$5k MRR side income" in r.text


def test_onboard_post_no_provider_re_renders_with_error(
    initialized_dir: Path,
) -> None:
    """No LLM provider → re-render the form with a friendly message instead
    of 500'ing on the conversion-critical first screen."""
    client = TestClient(build_app(), follow_redirects=False)
    r = client.post("/app/onboard", data={"answer": "I want $5k MRR in 6 months"})
    assert r.status_code == 200
    assert "No LLM provider configured" in r.text


def test_onboard_post_empty_answer_re_renders(initialized_dir: Path) -> None:
    """Empty submission shows a hint, doesn't 422 or hit the LLM."""
    client = TestClient(build_app(), follow_redirects=False)
    r = client.post("/app/onboard", data={"answer": "   "})
    assert r.status_code == 200
    assert "even a rough sentence" in r.text


def test_onboard_done_redirects_when_no_brief(initialized_dir: Path) -> None:
    """Direct hit on /onboard/done before submitting → bounces to step 1
    instead of rendering an empty 'thanks' page."""
    client = TestClient(build_app(), follow_redirects=False)
    r = client.get("/app/onboard/done")
    assert r.status_code == 303
    assert r.headers["location"] == "/app/onboard"


def _set_brief(initialized_dir: Path, brief: dict[str, object]) -> None:
    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        biz.founder_brief = brief
        session.add(biz)
        session.commit()


def test_onboard_done_renders_with_brief(initialized_dir: Path) -> None:
    """With a captured brief, /onboard/done shows the summary and the
    HTMX-driven niche-results placeholder."""
    _set_brief(
        initialized_dir,
        {
            "goal": "$5k MRR in 6 months",
            "summary": "You want $5k MRR in 6 months. Next: pick a niche.",
            "time_per_week_hours": 10,
            "savings_usd": 2000,
            "timeline_months": 6,
        },
    )
    client = TestClient(build_app(), follow_redirects=False)
    r = client.get("/app/onboard/done")
    assert r.status_code == 200
    assert "$5k MRR in 6 months" in r.text
    # HTMX trigger that auto-loads the niche fragment
    assert "/app/onboard/niche-fragment" in r.text
    assert 'hx-trigger="load"' in r.text


def test_niche_fragment_no_provider_returns_inline_error(
    initialized_dir: Path,
) -> None:
    """When the LLM provider isn't configured the fragment returns an
    inline error block, not a 503 page — the HTMX swap should keep the
    user on the onboard flow."""
    _set_brief(
        initialized_dir,
        {"goal": "$5k MRR", "summary": "x"},
    )
    client = TestClient(build_app(), follow_redirects=False)
    r = client.get("/app/onboard/niche-fragment")
    assert r.status_code == 200
    assert "No LLM provider configured" in r.text


def test_pick_niche_creates_goal_task_and_redirects(initialized_dir: Path) -> None:
    """Picking a niche creates a Goal, seeds the first validation Task,
    flips Business.status to VALIDATING, and 303s to the dashboard.

    The seeded Task is the whole point — it makes the dashboard non-empty
    on minute one of the relationship.
    """
    from sqlmodel import select

    from korpha.business.model import BusinessStatus, Goal, Task, TaskStatus

    _set_brief(initialized_dir, {"goal": "$5k MRR", "summary": "x"})
    client = TestClient(build_app(), follow_redirects=False)
    r = client.post(
        "/app/onboard/pick-niche",
        data={
            "name": "Deployment automation for solo Python devs",
            "value_prop": "Removes 5h/wk of devops",
            "validation_experiment": "5 interviews + 1-page Carrd landing",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/dashboard"

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(select(Business)).first()
        assert biz is not None
        assert biz.status == BusinessStatus.VALIDATING
        goals = list(session.exec(select(Goal).where(Goal.business_id == biz.id)).all())
        assert len(goals) == 1
        assert "Deployment automation" in goals[0].title
        assert goals[0].description == "Removes 5h/wk of devops"

        tasks = list(session.exec(select(Task).where(Task.business_id == biz.id)).all())
        assert len(tasks) == 1
        assert "5 interviews" in tasks[0].title
        assert tasks[0].status == TaskStatus.PENDING
        assert tasks[0].ref_number == 1


def _create_landing_approval(initialized_dir: Path) -> str:
    """Helper: insert a landing-copy approval and return its id."""
    from korpha.approvals.model import ActionClass, Approval, ApprovalStatus
    from korpha.cofounder.hiring import HiringService

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        ceo = HiringService(session).ensure_ceo(biz.id)
        approval = Approval(
            business_id=biz.id,
            agent_role_id=ceo.id,
            action_class=ActionClass.PUBLIC_POST,
            proposal_summary="Landing copy review",
            action_payload={
                "kind": "landing_copy",
                "niche_name": "Deployment automation",
                "result": {
                    "headline": "Stop fixing deploys at 2am",
                    "subhead": "Your cofounder ships while you sleep",
                    "primary_cta": "Get early access",
                    "social_proof_line": "Used by 50 indie devs",
                    "objection_handlers": [
                        {"objection": "Too expensive?", "response": "$29/mo, cancel anytime"},
                    ],
                },
            },
            status=ApprovalStatus.PENDING,
        )
        session.add(approval)
        session.commit()
        session.refresh(approval)
        return str(approval.id)


def test_landing_preview_renders_real_page(initialized_dir: Path) -> None:
    """The preview route returns the full landing page (not the
    dashboard chrome) so the Founder sees what they're approving."""
    approval_id = _create_landing_approval(initialized_dir)
    client = TestClient(build_app())
    r = client.get(f"/app/approvals/{approval_id}/preview")
    assert r.status_code == 200
    body = r.text
    assert "Stop fixing deploys at 2am" in body
    assert "Your cofounder ships while you sleep" in body
    assert "Get early access" in body
    assert "Used by 50 indie devs" in body
    assert "Too expensive?" in body
    # Standalone page — the dashboard sidebar should NOT be present
    assert "nav-item" not in body
    # The "back to approvals" strip should be there
    assert "back to approvals" in body


def test_landing_preview_404_for_unknown_id(initialized_dir: Path) -> None:
    from uuid import uuid4

    client = TestClient(build_app())
    r = client.get(f"/app/approvals/{uuid4()}/preview")
    assert r.status_code == 404


def test_validation_preview_renders_report(initialized_dir: Path) -> None:
    """Validation approvals get their own preview page that renders the
    score breakdown + verdict + kill test as styled cards."""
    from korpha.approvals.model import ActionClass, Approval, ApprovalStatus
    from korpha.cofounder.hiring import HiringService

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        ceo = HiringService(session).ensure_ceo(biz.id)
        approval = Approval(
            business_id=biz.id, agent_role_id=ceo.id,
            action_class=ActionClass.INTERNAL,
            proposal_summary="v",
            action_payload={
                "kind": "validation_report",
                "niche_name": "Deployment automation",
                "result": {
                    "scores": {
                        "demand_signal": 7,
                        "willingness_to_pay": 6,
                        "founder_fit": 9,
                        "distribution_path": 5,
                    },
                    "overall": 7,
                    "verdict": "go",
                    "strengths": ["Real pain", "You have the skills"],
                    "concerns": ["Crowded space"],
                    "kill_test": "5 interviews; if <2 say yes, kill",
                    "improvement_path": "",
                },
            },
            status=ApprovalStatus.PENDING,
        )
        session.add(approval)
        session.commit()
        session.refresh(approval)
        approval_id = str(approval.id)

    client = TestClient(build_app())
    r = client.get(f"/app/approvals/{approval_id}/preview")
    assert r.status_code == 200
    body = r.text
    assert "Deployment automation" in body
    assert "GO" in body  # verdict pill
    assert "7" in body  # overall score
    assert "Real pain" in body
    assert "Crowded space" in body
    assert "5 interviews" in body
    # Standalone — no dashboard chrome
    assert "nav-item" not in body


def test_outreach_preview_renders_email_stack(initialized_dir: Path) -> None:
    """Outreach approvals render as a stack of email-client mockups so
    the Founder can read each variant in context."""
    from korpha.approvals.model import ActionClass, Approval, ApprovalStatus
    from korpha.cofounder.hiring import HiringService

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        ceo = HiringService(session).ensure_ceo(biz.id)
        approval = Approval(
            business_id=biz.id, agent_role_id=ceo.id,
            action_class=ActionClass.EMAIL_OUTREACH,
            proposal_summary="o",
            action_payload={
                "kind": "outreach_drafts",
                "niche_name": "Deployment automation",
                "result": {
                    "variants": [
                        {
                            "angle": "shared-pain",
                            "subject": "Quick q on your last deploy",
                            "body": "Hi — saw your post about CI hating life.",
                        },
                        {
                            "angle": "curiosity",
                            "subject": "Counter-question",
                            "body": "What's your worst 2am page?",
                        },
                    ],
                    "personalization_template": "<recent post>",
                    "follow_up_subject": "circling back on deploys",
                },
            },
            status=ApprovalStatus.PENDING,
        )
        session.add(approval)
        session.commit()
        session.refresh(approval)
        approval_id = str(approval.id)

    client = TestClient(build_app())
    r = client.get(f"/app/approvals/{approval_id}/preview")
    assert r.status_code == 200
    body = r.text
    # Both variants visible
    assert "Quick q on your last deploy" in body
    assert "Counter-question" in body
    assert "shared-pain" in body
    assert "curiosity" in body
    # Footer metadata present
    assert "Per-prospect cue" in body
    assert "circling back on deploys" in body
    # Standalone — no dashboard chrome
    assert "nav-item" not in body


def test_preview_400_for_unknown_kind(initialized_dir: Path) -> None:
    """A payload with an unrecognized kind still 400s — we only support
    the four chain kinds we know how to render."""
    from korpha.approvals.model import ActionClass, Approval, ApprovalStatus
    from korpha.cofounder.hiring import HiringService

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        ceo = HiringService(session).ensure_ceo(biz.id)
        approval = Approval(
            business_id=biz.id, agent_role_id=ceo.id,
            action_class=ActionClass.COMMERCE,
            proposal_summary="x",
            action_payload={"kind": "create_payment_link", "amount_usd": 29.0},
            status=ApprovalStatus.PENDING,
        )
        session.add(approval)
        session.commit()
        session.refresh(approval)
        approval_id = str(approval.id)

    client = TestClient(build_app())
    r = client.get(f"/app/approvals/{approval_id}/preview")
    # Stripe approval → no preview view (the card already shows the
    # amount + name; the actual link is created at execute time)
    assert r.status_code == 400


def test_approval_formatter_surfaces_chain_kinds(initialized_dir: Path) -> None:
    """The chain creates approvals with a kind/result envelope. The
    formatter should pull the load-bearing field (verdict, headline,
    first variant) into the preview rather than hiding it behind JSON."""
    from korpha.api.dashboard import _format_approval
    from korpha.approvals.model import ActionClass, Approval, ApprovalStatus

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(
            __import__("sqlmodel").select(Business)  # type: ignore[attr-defined]
        ).first()
        assert biz is not None
        from korpha.cofounder.hiring import HiringService

        ceo = HiringService(session).ensure_ceo(biz.id)

        validation = Approval(
            business_id=biz.id, agent_role_id=ceo.id,
            action_class=ActionClass.INTERNAL,
            proposal_summary="v",
            action_payload={
                "kind": "validation_report",
                "niche_name": "X",
                "result": {
                    "overall": 8, "verdict": "go",
                    "kill_test": "5 interviews; if <2 say yes, kill",
                },
            },
            status=ApprovalStatus.PENDING,
        )
        landing = Approval(
            business_id=biz.id, agent_role_id=ceo.id,
            action_class=ActionClass.PUBLIC_POST,
            proposal_summary="l",
            action_payload={
                "kind": "landing_copy",
                "result": {
                    "headline": "Stop fixing deploys at 2am",
                    "subhead": "We do it for you",
                    "primary_cta": "Get early access",
                },
            },
            status=ApprovalStatus.PENDING,
        )
        outreach = Approval(
            business_id=biz.id, agent_role_id=ceo.id,
            action_class=ActionClass.EMAIL_OUTREACH,
            proposal_summary="o",
            action_payload={
                "kind": "outreach_drafts",
                "result": {
                    "variants": [
                        {"subject": "Quick q on deploys", "body": "Hi — saw your post..."},
                        {"subject": "Other", "body": "..."},
                    ],
                },
            },
            status=ApprovalStatus.PENDING,
        )

    val_fmt = _format_approval(validation)
    assert val_fmt["kind_tag"] == "validation report"
    keys = {row["key"] for row in val_fmt["preview_lines"]}
    assert {"Verdict", "Score", "Kill test"} <= keys
    assert any(row["value"] == "GO" for row in val_fmt["preview_lines"])
    assert any(row["value"] == "8/10" for row in val_fmt["preview_lines"])

    land_fmt = _format_approval(landing)
    assert land_fmt["kind_tag"] == "landing copy"
    assert any(
        row["value"] == "Stop fixing deploys at 2am"
        for row in land_fmt["preview_lines"]
    )
    assert any(row["value"] == "Get early access" for row in land_fmt["preview_lines"])

    out_fmt = _format_approval(outreach)
    assert out_fmt["kind_tag"] == "outreach drafts"
    assert any(
        row["value"] == "Quick q on deploys" for row in out_fmt["preview_lines"]
    )
    assert any(row["value"] == "2 drafts" for row in out_fmt["preview_lines"])


def test_pick_niche_no_provider_doesnt_crash(initialized_dir: Path) -> None:
    """When no LLM provider is configured the pick-niche route must
    still create the Goal/Task and redirect — the background chain
    just doesn't fire. We're testing graceful degradation here."""
    from sqlmodel import select

    from korpha.business.model import Goal

    _set_brief(initialized_dir, {"goal": "$5k MRR", "summary": "x"})
    client = TestClient(build_app(), follow_redirects=False)
    r = client.post(
        "/app/onboard/pick-niche",
        data={
            "name": "Niche A",
            "value_prop": "v",
            "validation_experiment": "e",
            "target_avatar": "a",
        },
    )
    assert r.status_code == 303
    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(select(Business)).first()
        assert biz is not None
        assert len(list(session.exec(select(Goal).where(Goal.business_id == biz.id)).all())) == 1


def test_pick_niche_without_experiment_skips_task(initialized_dir: Path) -> None:
    """Niche skill might omit validation_experiment for some candidates;
    the Goal still gets created, just no seeded Task."""
    from sqlmodel import select

    from korpha.business.model import Goal, Task

    _set_brief(initialized_dir, {"goal": "$5k MRR", "summary": "x"})
    client = TestClient(build_app(), follow_redirects=False)
    r = client.post(
        "/app/onboard/pick-niche",
        data={
            "name": "Some niche",
            "value_prop": "X",
            "validation_experiment": "",
        },
    )
    assert r.status_code == 303

    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        biz = session.exec(select(Business)).first()
        assert biz is not None
        assert len(list(session.exec(select(Goal).where(Goal.business_id == biz.id)).all())) == 1
        assert list(session.exec(select(Task).where(Task.business_id == biz.id)).all()) == []


def test_pick_niche_empty_name_redirects_back(initialized_dir: Path) -> None:
    """Empty name (defensive guard against malformed POSTs) bounces
    back to the proposal page rather than creating a junk Goal."""
    _set_brief(initialized_dir, {"goal": "$5k MRR", "summary": "x"})
    client = TestClient(build_app(), follow_redirects=False)
    r = client.post("/app/onboard/pick-niche", data={"name": "  ", "value_prop": ""})
    assert r.status_code == 303
    assert r.headers["location"] == "/app/onboard/done"
