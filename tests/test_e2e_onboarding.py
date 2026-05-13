"""End-to-end test of the Day-0 conversion flow.

Walks the full path with a mock LLM provider:

    GET  /app/dashboard       → 303 to /app/onboard (no brief yet)
    POST /app/onboard         → runs founder.intake_brief, redirects
    GET  /app/onboard/done    → renders, references niche-fragment
    GET  /app/onboard/niche-fragment → runs niche skill, returns cards
    POST /app/onboard/pick-niche → creates Goal+Task, fires bg chain,
                                   redirects to dashboard
    (background) chain creates 3 Approvals
    GET  /app/approvals       → lists the 3 approvals
    GET  /app/approvals/<id>/preview → renders landing as a page

This is the regression safety net for the BRIEF.md 5-minute demo path.
If this test breaks, the conversion-critical first-run experience is
broken and we want to know before shipping.
"""
from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

import korpha.db.registry  # noqa: F401  -- registers all models
from korpha.api import build_app
from korpha.approvals.model import Approval
from korpha.business.model import Business, BusinessStatus, Goal, Task
from korpha.cofounder.hiring import HiringService
from korpha.identity.model import Founder
from korpha.inference.providers.mock import MockProvider

_INTAKE = (
    '{"goal":"$5k MRR in 6 months","timeline_months":6,'
    '"time_per_week_hours":10,"savings_usd":2000,'
    '"skills":"Python, Django","niches_considered":["devops"],'
    '"constraints":["cant quit job"],'
    '"summary":"You want $5k MRR in 6 months. Pick a deployment niche."}'
)
_NICHE = (
    '{"candidates":['
    '{"name":"Deployment automation for solo Python devs",'
    '"target_avatar":"indie hackers shipping side SaaS",'
    '"value_prop":"Removes 5h/wk of devops",'
    '"price_band":"$29-99/mo","competition":"render covers ops",'
    '"validation_experiment":"5 interviews + 1-page Carrd landing",'
    '"fit_score":9}],'
    '"recommended_index":0,"rationale":"highest fit"}'
)
_VALIDATE = (
    '{"scores":{"demand_signal":7,"willingness_to_pay":7,'
    '"founder_fit":9,"distribution_path":6},"overall":7,'
    '"verdict":"go","strengths":["s"],"concerns":["c"],'
    '"kill_test":"5 interviews; if <2 say yes, kill",'
    '"improvement_path":""}'
)
_LANDING = (
    '{"headline":"Stop fixing deploys at 2am",'
    '"subhead":"Your cofounder ships while you sleep",'
    '"social_proof_line":"Used by 50 indie devs",'
    '"primary_cta":"Get early access","cta_verb":"Sign up",'
    '"objection_handlers":[{"objection":"Too expensive?",'
    '"response":"$29/mo, cancel anytime"}],'
    '"meta_description":"M"}'
)
_OUTREACH = (
    '{"variants":[{"angle":"shared-pain",'
    '"subject":"Quick q on your last deploy",'
    '"body":"Hi — I saw your post about hating CI. I built..."}],'
    '"personalization_template":"<recent post>",'
    '"follow_up_subject":"following up on deploys"}'
)


class _ScriptedProvider(MockProvider):
    """MockProvider variant that walks a script of canned responses,
    sticking on the last one once exhausted (defensive — keeps tests
    from crashing if a chain runs more skill calls than scripted)."""

    def __init__(self, responses: list[str]):
        super().__init__(static_response=responses[0])
        self._responses = responses
        self._idx = 0

    async def complete(self, request, account):  # type: ignore[override]
        idx = min(self._idx, len(self._responses) - 1)
        self._idx += 1
        original = self.static_response
        self.static_response = self._responses[idx]
        try:
            return await super().complete(request, account)
        finally:
            self.static_response = original


@pytest.fixture
def temp_data_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        monkeypatch.setenv("KORPHA_DATA_DIR", str(path))
        # We DO need a provider key to be considered configured. Use a
        # fake; we'll patch the pool builder to return our scripted
        # provider regardless of which key is set.
        monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "fake-for-test")
        yield path


@pytest.fixture
def initialized_dir(temp_data_dir: Path) -> Path:
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
        biz = Business(founder_id=founder.id, name="WidgetCo")
        session.add(biz)
        session.commit()
        session.refresh(biz)
        HiringService(session).ensure_ceo(biz.id)
    return temp_data_dir


@pytest.fixture
def patched_pool(monkeypatch: pytest.MonkeyPatch) -> _ScriptedProvider:
    """Replace _build_pool_pieces with one that returns our scripted
    provider — independent of env vars / providers.yaml."""
    from korpha.audit.model import InferenceTier
    from korpha.inference import ProviderAccount
    from korpha.inference.registry import AuthType

    provider = _ScriptedProvider(
        [_INTAKE, _NICHE, _VALIDATE, _LANDING, _OUTREACH]
    )
    account = ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "mock-flash",
            InferenceTier.PRO: "mock-pro",
        },
        api_key="x",
    )

    def fake_pool_pieces():
        return [provider], [account]

    import korpha.api.server as srv

    monkeypatch.setattr(srv, "_build_pool_pieces", fake_pool_pieces)
    return provider


def test_full_day_zero_flow(initialized_dir: Path, patched_pool) -> None:
    db_path = initialized_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    client = TestClient(build_app(), follow_redirects=False)

    # 1. Fresh dashboard hit redirects to onboard
    r = client.get("/app/dashboard")
    assert r.status_code == 303
    assert r.headers["location"] == "/app/onboard"

    # 2. Submit the brief
    r = client.post(
        "/app/onboard",
        data={"answer": "I'm a Python dev, want $5k/mo in 6 months"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/onboard/done"

    with Session(engine) as session:
        biz = session.exec(select(Business)).first()
        assert biz is not None
        assert biz.founder_brief.get("goal") == "$5k MRR in 6 months"

    # 3. Done page renders + niche fragment runs the niche skill
    r = client.get("/app/onboard/done")
    assert r.status_code == 200
    assert "$5k MRR in 6 months" in r.text

    r = client.get("/app/onboard/niche-fragment")
    assert r.status_code == 200
    assert "Deployment automation for solo Python devs" in r.text
    assert "Go with this →" in r.text  # recommended button

    # 4. Pick the niche — fires bg chain
    r = client.post(
        "/app/onboard/pick-niche",
        data={
            "name": "Deployment automation for solo Python devs",
            "value_prop": "Removes 5h/wk of devops",
            "validation_experiment": "5 interviews + 1-page Carrd landing",
            "target_avatar": "indie hackers shipping side SaaS",
            "price_band": "$29-99/mo",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/dashboard"

    # 5. After bg task drains, business has Goal + Task + 3 Approvals
    with Session(engine) as session:
        biz = session.exec(select(Business)).first()
        assert biz is not None
        assert biz.status == BusinessStatus.VALIDATING
        goals = list(session.exec(select(Goal).where(Goal.business_id == biz.id)).all())
        assert len(goals) == 1
        assert "Deployment automation" in goals[0].title

        tasks = list(session.exec(select(Task).where(Task.business_id == biz.id)).all())
        assert len(tasks) == 1
        assert tasks[0].ref_number == 1

        approvals = list(
            session.exec(select(Approval).where(Approval.business_id == biz.id)).all()
        )
        # 4 chain-shaped approvals (validation, landing, outreach, calendar
        # kickoff invite) + 1 Stripe (commerce.create_payment_link adds its
        # own approval directly via the skill's run method).
        assert len(approvals) == 5
        kinds = {a.action_payload.get("kind") for a in approvals}
        assert kinds == {
            "validation_report", "landing_copy", "outreach_drafts",
            "create_payment_link", "calendar_invite",
        }
        # Stripe link uses the lower bound of the price band
        stripe = next(a for a in approvals if a.action_payload.get("kind") == "create_payment_link")
        assert stripe.action_payload["amount_usd"] == 29.0

    # 6. Dashboard now renders (brief is present)
    r = client.get("/app/dashboard")
    assert r.status_code == 200
    assert "WidgetCo" in r.text

    # 7. Approvals view lists all 3 with the new kind tags + previews
    r = client.get("/app/approvals")
    assert r.status_code == 200
    body = r.text
    assert "VALIDATION REPORT" in body.upper() or "validation report" in body
    assert "LANDING COPY" in body.upper() or "landing copy" in body
    assert "OUTREACH DRAFTS" in body.upper() or "outreach drafts" in body

    # Pull the landing approval's id and verify the preview route works
    with Session(engine) as session:
        biz = session.exec(select(Business)).first()
        assert biz is not None
        landing = session.exec(
            select(Approval).where(Approval.business_id == biz.id)
        ).all()
        landing_approval = next(
            a for a in landing
            if (a.action_payload or {}).get("kind") == "landing_copy"
        )
        landing_id = landing_approval.id

    r = client.get(f"/app/approvals/{landing_id}/preview")
    assert r.status_code == 200
    assert "Stop fixing deploys at 2am" in r.text
    assert "Your cofounder ships while you sleep" in r.text
    assert "Get early access" in r.text
