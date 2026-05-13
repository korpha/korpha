"""Stripe client + commerce.create_payment_link skill tests."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlmodel import Session, select

from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
)
from korpha.audit.model import Activity
from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.commerce import PaymentLink, StripeClient, StripeError
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    MockProvider,
    ProviderAccount,
    TierPricing,
)
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError

# ───────────────────────── Stripe client ─────────────────────────


def _stripe_handler(
    *, fail_on: str | None = None, error_message: str = "test error"
) -> httpx.MockTransport:
    """Build a MockTransport that simulates Stripe's product/price/link
    creation flow. ``fail_on`` is one of {'products', 'prices',
    'payment_links'} to trigger a 4xx on that endpoint."""

    state: dict[str, object] = {
        "product_id": "prod_test_123",
        "price_id": "price_test_456",
        "link_id": "plink_test_789",
        "link_url": "https://buy.stripe.com/test_xyz",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/products"):
            if fail_on == "products":
                return httpx.Response(
                    400, json={"error": {"message": error_message}}
                )
            return httpx.Response(200, json={"id": state["product_id"]})
        if path.endswith("/prices"):
            if fail_on == "prices":
                return httpx.Response(
                    400, json={"error": {"message": error_message}}
                )
            return httpx.Response(200, json={"id": state["price_id"]})
        if path.endswith("/payment_links"):
            if fail_on == "payment_links":
                return httpx.Response(
                    400, json={"error": {"message": error_message}}
                )
            return httpx.Response(
                200,
                json={"id": state["link_id"], "url": state["link_url"]},
            )
        return httpx.Response(404, json={"error": {"message": "no"}})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_stripe_create_payment_link_happy_path() -> None:
    client = StripeClient(api_key="sk_test_x")
    client._client = httpx.AsyncClient(transport=_stripe_handler())
    link = await client.create_payment_link(
        name="Niche Finder", amount_usd=29.0, description="3-5 micro-niches"
    )
    await client.close()
    assert isinstance(link, PaymentLink)
    assert link.url.startswith("https://buy.stripe.com/")
    assert link.amount_minor == 2900  # cents
    assert link.currency == "usd"
    assert link.product_id == "prod_test_123"


@pytest.mark.asyncio
async def test_stripe_zero_amount_rejected() -> None:
    client = StripeClient(api_key="sk_test_x")
    with pytest.raises(StripeError):
        await client.create_payment_link(name="x", amount_usd=0)


@pytest.mark.asyncio
async def test_stripe_propagates_4xx_error() -> None:
    client = StripeClient(api_key="sk_test_x")
    client._client = httpx.AsyncClient(
        transport=_stripe_handler(
            fail_on="prices", error_message="invalid currency"
        )
    )
    with pytest.raises(StripeError) as exc:
        await client.create_payment_link(name="x", amount_usd=10)
    assert "invalid currency" in str(exc.value)
    await client.close()


@pytest.mark.asyncio
async def test_stripe_amount_minor_unit_rounding() -> None:
    client = StripeClient(api_key="sk_test_x")
    client._client = httpx.AsyncClient(transport=_stripe_handler())
    link = await client.create_payment_link(name="x", amount_usd=4.995)
    # 4.995 * 100 = 499.5 → rounds to 500 cents
    assert link.amount_minor == 500
    await client.close()


# ───────────────────────── Skill ─────────────────────────


def _make_ctx(
    session: Session, business: Business, founder: Founder
) -> SkillContext:
    pool = InferencePool(
        providers=[MockProvider()],
        accounts=[
            ProviderAccount(
                provider_name="mock",
                auth_type=AuthType.API_KEY,
                tier_models={"pro": "mock-pro"},  # type: ignore[arg-type]
                pricing={
                    "pro": TierPricing(  # type: ignore[dict-item]
                        input_per_1m_usd=Decimal("0.5"),
                        output_per_1m_usd=Decimal("1"),
                    ),
                },
                api_key="sk",
            )
        ],
    )
    tracker = CostTracker(pool=pool)
    ceo = HiringService(session).ensure_ceo(business.id)
    return SkillContext(
        business=business,
        founder=founder,
        session=session,
        cost_tracker=tracker,
        invoking_agent_role_id=ceo.id,
    )


@pytest.mark.asyncio
async def test_create_payment_link_skill_creates_pending_approval(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("commerce.create_payment_link")
    result = await skill.run(
        ctx=ctx,
        args={
            "name": "Niche Finder",
            "amount_usd": 29.0,
            "description": "3-5 micro-niches you can validate this week",
            "currency": "usd",
        },
    )
    assert result.payload["status"] == "pending"
    rows = list(
        session.exec(
            select(Approval)
            .where(Approval.business_id == business.id)
            .where(Approval.action_class == ActionClass.COMMERCE)
        ).all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status == ApprovalStatus.PENDING
    assert row.platform == "stripe"
    assert row.action_payload["amount_usd"] == 29.0
    assert row.action_payload["currency"] == "usd"
    assert row.action_payload["kind"] == "create_payment_link"


@pytest.mark.asyncio
async def test_create_payment_link_logs_proposal_activity(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("commerce.create_payment_link")
    await skill.run(
        ctx=ctx, args={"name": "Test Product", "amount_usd": 5.0}
    )
    events = list(
        session.exec(
            select(Activity)
            .where(Activity.business_id == business.id)
            .where(Activity.event_type == "commerce.payment_link_proposed")
        ).all()
    )
    assert len(events) == 1
    assert events[0].payload["amount_usd"] == 5.0


@pytest.mark.asyncio
async def test_create_payment_link_rejects_zero_amount(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("commerce.create_payment_link")
    with pytest.raises(SkillError):
        await skill.run(ctx=ctx, args={"name": "x", "amount_usd": 0})


@pytest.mark.asyncio
async def test_create_payment_link_rejects_unknown_currency(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("commerce.create_payment_link")
    with pytest.raises(SkillError) as exc:
        await skill.run(
            ctx=ctx,
            args={"name": "Valid Name", "amount_usd": 10, "currency": "btc"},
        )
    assert "btc" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_create_payment_link_rejects_short_name(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("commerce.create_payment_link")
    with pytest.raises(SkillError):
        await skill.run(ctx=ctx, args={"name": "x", "amount_usd": 10})


@pytest.mark.asyncio
async def test_create_payment_link_truncates_long_description(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("commerce.create_payment_link")
    long = "lorem ipsum " * 80  # ~960 chars
    result = await skill.run(
        ctx=ctx,
        args={"name": "Test", "amount_usd": 10, "description": long},
    )
    assert len(result.payload["description"]) <= 500
    assert result.payload["description"].endswith("…")
