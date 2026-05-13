"""Post-pick-niche skill chain tests.

Uses mock LLM responses for each of the three skills the chain runs.
The chain opens its own session via the engine, so tests build a real
SQLite engine and assert against rows written there.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

import korpha.db.registry  # noqa: F401  -- registers all models
from korpha.approvals.model import ActionClass, Approval
from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.identity.model import Founder
from korpha.inference import InferencePool, MockProvider, ProviderAccount
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType
from korpha.onboarding.chain import run_post_pick_niche_chain

_VALIDATE_OK = (
    '{"scores":{"demand_signal":7,"willingness_to_pay":6,'
    '"founder_fit":8,"distribution_path":5},"overall":7,'
    '"verdict":"go","strengths":["s"],"concerns":["c"],'
    '"kill_test":"k","improvement_path":""}'
)
_LANDING_OK = (
    '{"headline":"H","subhead":"S","social_proof_line":"P",'
    '"primary_cta":"C","cta_verb":"Go",'
    '"objection_handlers":[{"objection":"o","response":"r"}],'
    '"meta_description":"M"}'
)
_OUTREACH_OK = (
    '{"variants":[{"angle":"a","subject":"s","body":"b"}],'
    '"personalization_template":"p","follow_up_subject":"f"}'
)


class _CycleProvider(MockProvider):
    """MockProvider variant that returns different canned responses on
    each call. The chain runs three skills in sequence; this lets us
    feed each skill its own response shape."""

    def __init__(self, responses: list[str]):
        # Use the first response as the parent's static so model bookkeeping
        # works; the cycler below overrides per-call.
        super().__init__(static_response=responses[0] if responses else "{}")
        self._responses = list(responses)
        self._idx = 0

    async def complete(self, request, account):  # type: ignore[override]
        # Return responses round-robin, sticking on the last one if we
        # run past the end (defensive — tests should provide enough).
        idx = min(self._idx, len(self._responses) - 1)
        self._idx += 1
        # Build by delegating to parent with the swapped static_response
        original = self.static_response
        self.static_response = self._responses[idx]
        try:
            return await super().complete(request, account)
        finally:
            self.static_response = original


def _account() -> ProviderAccount:
    return ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "mock-flash",
            InferenceTier.PRO: "mock-pro",
        },
        api_key="x",
    )


def _make_engine_with_business(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        founder = Founder(email="m@x.com", display_name="Mike")
        session.add(founder)
        session.commit()
        session.refresh(founder)
        business = Business(founder_id=founder.id, name="Test Co")
        session.add(business)
        session.commit()
        session.refresh(business)
        HiringService(session).ensure_ceo(business.id)
        # Capture id before session closes — accessing business.id after
        # the session exits triggers a refresh against a closed session.
        biz_id = business.id
    return engine, biz_id


@pytest.mark.asyncio
async def test_chain_creates_four_approvals_on_happy_path(tmp_path) -> None:
    """Validate + landing + outreach + calendar kickoff invite = 4 approvals
    on the happy path (no price_band, so Stripe doesn't fire)."""
    engine, business_id = _make_engine_with_business(tmp_path)
    provider = _CycleProvider([_VALIDATE_OK, _LANDING_OK, _OUTREACH_OK])

    def factory() -> CostTracker:
        pool = InferencePool(providers=[provider], accounts=[_account()])
        return CostTracker(pool=pool)

    report = await run_post_pick_niche_chain(
        engine=engine,
        business_id=business_id,
        niche={
            "name": "Deployment automation for solo Python devs",
            "value_prop": "Removes 5h/wk of devops",
            "target_avatar": "indie hackers shipping side SaaS",
        },
        cost_tracker_factory=factory,
    )
    assert report["approvals_created"] == 4
    assert report["errors"] == []

    with Session(engine) as session:
        approvals = list(
            session.exec(
                select(Approval).where(Approval.business_id == business_id)
            ).all()
        )
        assert len(approvals) == 4
        action_classes = {a.action_class for a in approvals}
        assert ActionClass.INTERNAL in action_classes  # validation + calendar
        assert ActionClass.PUBLIC_POST in action_classes  # landing
        assert ActionClass.EMAIL_OUTREACH in action_classes  # outreach
        kinds = {a.action_payload.get("kind") for a in approvals}
        assert "calendar_invite" in kinds
        # All start as PENDING — Founder hasn't acted yet
        assert all(a.status.value == "pending" for a in approvals)
        # Niche name flows through into proposal_summary
        assert any("Deployment automation" in a.proposal_summary for a in approvals)


@pytest.mark.asyncio
async def test_chain_continues_when_one_skill_fails(tmp_path) -> None:
    """validate.score_idea returns garbage → its Approval is skipped, but
    landing + outreach still produce theirs. The chain is tolerant by
    design — the Founder still wins partial output."""
    engine, business_id = _make_engine_with_business(tmp_path)
    provider = _CycleProvider(["not json at all", _LANDING_OK, _OUTREACH_OK])

    def factory() -> CostTracker:
        pool = InferencePool(providers=[provider], accounts=[_account()])
        return CostTracker(pool=pool)

    report = await run_post_pick_niche_chain(
        engine=engine,
        business_id=business_id,
        niche={"name": "X niche", "value_prop": "y", "target_avatar": "z"},
        cost_tracker_factory=factory,
    )
    # validate fails → 0; landing + outreach + calendar succeed → 3.
    assert report["approvals_created"] == 3
    assert len(report["errors"]) == 1
    assert "validate" in report["errors"][0]


@pytest.mark.asyncio
async def test_chain_creates_payment_link_when_price_band_given(tmp_path) -> None:
    """When the picked niche carries a parseable price_band, the chain
    also drafts a Stripe payment link → 4 approvals total."""
    engine, business_id = _make_engine_with_business(tmp_path)
    provider = _CycleProvider([_VALIDATE_OK, _LANDING_OK, _OUTREACH_OK])

    def factory() -> CostTracker:
        pool = InferencePool(providers=[provider], accounts=[_account()])
        return CostTracker(pool=pool)

    report = await run_post_pick_niche_chain(
        engine=engine,
        business_id=business_id,
        niche={
            "name": "Deployment automation",
            "value_prop": "v",
            "target_avatar": "a",
            "price_band": "$29-99/mo",
        },
        cost_tracker_factory=factory,
    )
    # 3 LLM-skill approvals + 1 Stripe approval + 1 calendar kickoff invite
    assert report["approvals_created"] == 5

    with Session(engine) as session:
        approvals = list(
            session.exec(
                select(Approval).where(Approval.business_id == business_id)
            ).all()
        )
        assert len(approvals) == 5
        stripe = [a for a in approvals if a.action_class == ActionClass.COMMERCE]
        assert len(stripe) == 1
        # The lower bound of "$29-99" is what we charge first.
        assert stripe[0].action_payload["amount_usd"] == 29.0
        assert stripe[0].action_payload["name"] == "Deployment automation"


@pytest.mark.asyncio
async def test_chain_skips_stripe_when_price_band_unparseable(tmp_path) -> None:
    """No price_band → no Stripe approval, but the rest of the chain
    runs normally. Defensive — niche skill might omit the field."""
    engine, business_id = _make_engine_with_business(tmp_path)
    provider = _CycleProvider([_VALIDATE_OK, _LANDING_OK, _OUTREACH_OK])

    def factory() -> CostTracker:
        pool = InferencePool(providers=[provider], accounts=[_account()])
        return CostTracker(pool=pool)

    report = await run_post_pick_niche_chain(
        engine=engine,
        business_id=business_id,
        niche={
            "name": "X",
            "value_prop": "y",
            "target_avatar": "z",
            "price_band": "ask",  # no number to parse
        },
        cost_tracker_factory=factory,
    )
    # validate + landing + outreach + calendar; price_band unparseable → no Stripe
    assert report["approvals_created"] == 4
    assert report["errors"] == []


def test_parse_price_lower_bound() -> None:
    from korpha.onboarding.chain import _parse_price_lower_bound

    assert _parse_price_lower_bound("$29-99/mo") == 29.0
    assert _parse_price_lower_bound("$99/mo") == 99.0
    assert _parse_price_lower_bound("$29.50") == 29.5
    assert _parse_price_lower_bound("USD 49 to 199") == 49.0
    assert _parse_price_lower_bound("free") is None
    assert _parse_price_lower_bound("") is None
    assert _parse_price_lower_bound("$0") is None  # zero is not a real price


@pytest.mark.asyncio
async def test_chain_no_op_on_empty_niche_name(tmp_path) -> None:
    """Defensive: empty niche name short-circuits without touching the LLM."""
    engine, business_id = _make_engine_with_business(tmp_path)

    def factory() -> CostTracker:
        return CostTracker(
            pool=InferencePool(providers=[MockProvider()], accounts=[_account()])
        )

    report = await run_post_pick_niche_chain(
        engine=engine,
        business_id=business_id,
        niche={"name": ""},
        cost_tracker_factory=factory,
    )
    assert report["approvals_created"] == 0
    assert report["errors"] == ["empty niche name"]
