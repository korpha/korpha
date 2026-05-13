"""outreach.send_cold_email skill tests."""
from __future__ import annotations

from decimal import Decimal

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


def _make_ctx(
    session: Session, business: Business, founder: Founder
) -> SkillContext:
    pool = InferencePool(
        providers=[MockProvider()],
        accounts=[
            ProviderAccount(
                provider_name="mock",
                auth_type=AuthType.API_KEY,
                tier_models={
                    "workhorse": "mock-flash",
                    "pro": "mock-pro",
                },  # type: ignore[arg-type]
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
async def test_send_cold_email_creates_pending_approval(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("outreach.send_cold_email")

    result = await skill.run(
        ctx=ctx,
        args={
            "to": "prospect@example.com",
            "subject": "Quick favor — 10 min interview",
            "body": (
                "Hey there — I'm building a niche-discovery tool for solo "
                "Python devs. Would love your perspective. 10 min Zoom?"
            ),
        },
    )
    assert result.skill_name == "outreach.send_cold_email"
    assert result.payload["status"] == "pending"
    assert result.payload["to"] == "prospect@example.com"

    rows = list(session.exec(select(Approval).where(Approval.business_id == business.id)).all())
    assert len(rows) == 1
    assert rows[0].action_class == ActionClass.EMAIL_OUTREACH
    assert rows[0].status == ApprovalStatus.PENDING
    assert rows[0].action_payload["to"] == "prospect@example.com"
    assert "10 min interview" in rows[0].action_payload["subject"]


@pytest.mark.asyncio
async def test_send_cold_email_rejects_bad_to(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("outreach.send_cold_email")
    with pytest.raises(SkillError) as exc:
        await skill.run(
            ctx=ctx,
            args={
                "to": "not-an-email",
                "subject": "x",
                "body": "long enough body content here ok",
            },
        )
    assert "valid email" in str(exc.value)


@pytest.mark.asyncio
async def test_send_cold_email_rejects_short_body(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("outreach.send_cold_email")
    with pytest.raises(SkillError):
        await skill.run(
            ctx=ctx,
            args={
                "to": "x@y.com",
                "subject": "x",
                "body": "too short",
            },
        )


@pytest.mark.asyncio
async def test_send_cold_email_rejects_missing_subject(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("outreach.send_cold_email")
    with pytest.raises(SkillError):
        await skill.run(
            ctx=ctx,
            args={
                "to": "x@y.com",
                "subject": "",
                "body": "long enough body content here ok",
            },
        )


@pytest.mark.asyncio
async def test_send_cold_email_logs_proposal_activity(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("outreach.send_cold_email")
    await skill.run(
        ctx=ctx,
        args={
            "to": "x@y.com",
            "subject": "Hi",
            "body": "Hello there friend let me share something with you",
        },
    )
    events = list(
        session.exec(
            select(Activity)
            .where(Activity.business_id == business.id)
            .where(Activity.event_type == "email.proposed")
        ).all()
    )
    assert len(events) == 1
    assert events[0].payload["to"] == "x@y.com"


@pytest.mark.asyncio
async def test_send_cold_email_preview_truncates_long_body(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder)
    skill = default_registry.get("outreach.send_cold_email")
    long_body = "lorem ipsum " * 80  # ~960 chars
    result = await skill.run(
        ctx=ctx,
        args={"to": "x@y.com", "subject": "Hi", "body": long_body},
    )
    preview = result.payload["body_preview"]
    assert len(preview) <= 250  # 240 + ellipsis margin
    assert preview.endswith("…")
