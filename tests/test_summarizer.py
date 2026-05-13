"""MemorySummarizer + compose() tests using the offline mock provider."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.cofounder.fts import ensure_fts_index
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.memory import MemoryService
from korpha.cofounder.model import (
    Message,
    MessageSenderType,
    MessageSummary,
    Thread,
    ThreadPlatform,
)
from korpha.cofounder.summarizer import MemorySummarizer
from korpha.db._base import utcnow
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    MockProvider,
    ProviderAccount,
    TierPricing,
)
from korpha.inference.registry import AuthType
from korpha.inference.types import Role


def _account() -> ProviderAccount:
    return ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.WORKHORSE: "mock-flash"},
        pricing={
            InferenceTier.WORKHORSE: TierPricing(
                input_per_1m_usd=Decimal("0.10"),
                output_per_1m_usd=Decimal("0.20"),
            ),
        },
        api_key="sk-test",
        label="primary",
    )


def _make_thread_with_messages(
    session: Session,
    business: Business,
    founder: Founder,
    contents: list[str],
) -> Thread:
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    thread = Thread(
        business_id=business.id,
        founder_id=founder.id,
        agent_role_id=ceo.id,
        platform=ThreadPlatform.WEB,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)

    base = utcnow() - timedelta(days=10)
    for i, c in enumerate(contents):
        m = Message(
            thread_id=thread.id,
            sender_type=(
                MessageSenderType.FOUNDER if i % 2 == 0 else MessageSenderType.AGENT
            ),
            content=c,
        )
        m.created_at = base + timedelta(hours=i)
        session.add(m)
    session.commit()
    return thread


@pytest.mark.asyncio
async def test_summarizer_creates_summary_row(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _make_thread_with_messages(
        session,
        business,
        founder,
        [
            "Founder: I want to ship a landing page",
            "I'd recommend tailwind + cloudflare pages",
            "Then a pricing page and a Stripe link",
            "ok we settled on a $29/mo single-tier launch",
        ],
    )

    provider = MockProvider(static_response="• decision: $29/mo single tier\n• stack: tailwind + cloudflare")
    pool = InferencePool(providers=[provider], accounts=[_account()])
    summarizer = MemorySummarizer(session=session, pool=pool)

    cutoff = utcnow()
    result = await summarizer.summarize_older(
        thread_id=thread.id, cutoff=cutoff, session_key="test"
    )
    assert result is not None
    assert result.summary.message_count == 4
    assert "tailwind" in result.summary.summary_text

    rows = session.exec(select(MessageSummary)).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_summarizer_skips_already_covered(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _make_thread_with_messages(
        session, business, founder, ["a", "b", "c"]
    )
    provider = MockProvider(static_response="summary one")
    pool = InferencePool(providers=[provider], accounts=[_account()])
    summarizer = MemorySummarizer(session=session, pool=pool)

    cutoff = utcnow()
    first = await summarizer.summarize_older(
        thread_id=thread.id, cutoff=cutoff, session_key="t"
    )
    assert first is not None

    # Running again with no new messages → None.
    second = await summarizer.summarize_older(
        thread_id=thread.id, cutoff=cutoff, session_key="t"
    )
    assert second is None


@pytest.mark.asyncio
async def test_summarizer_skips_empty_response(
    session: Session, business: Business, founder: Founder
) -> None:
    thread = _make_thread_with_messages(session, business, founder, ["hi"])
    provider = MockProvider(static_response="   ")
    pool = InferencePool(providers=[provider], accounts=[_account()])
    summarizer = MemorySummarizer(session=session, pool=pool)
    result = await summarizer.summarize_older(
        thread_id=thread.id, cutoff=utcnow(), session_key="t"
    )
    assert result is None
    assert session.exec(select(MessageSummary)).all() == []


def test_compose_returns_summary_plus_recent_plus_fts_hits(
    session: Session, business: Business, founder: Founder
) -> None:
    ensure_fts_index(session)
    thread = _make_thread_with_messages(
        session,
        business,
        founder,
        [
            "founder asked about pricing tiers",
            "we picked $29/mo single tier",
            "old discussion of Stripe vs Paddle",
            "modern question about analytics",
        ],
    )
    # Add a summary directly.
    summary = MessageSummary(
        thread_id=thread.id,
        summary_text="summary: founder picked $29/mo single tier",
        covers_until=utcnow() - timedelta(days=5),
        message_count=2,
    )
    session.add(summary)
    session.commit()

    mem = MemoryService(session=session, fts_top_k=2)
    result = mem.compose(
        business_id=business.id,
        founder_id=founder.id,
        query="Stripe",
        platform=ThreadPlatform.WEB,
    )
    assert len(result) >= 2
    # First message is the summary.
    assert result[0].role == Role.SYSTEM
    assert "$29/mo" in result[0].content
    # Some message must reference Stripe (FTS5 hit).
    assert any("Stripe" in m.content for m in result)


def test_compose_without_query_skips_fts(
    session: Session, business: Business, founder: Founder
) -> None:
    ensure_fts_index(session)
    _make_thread_with_messages(
        session, business, founder, ["one", "two", "three"]
    )
    mem = MemoryService(session=session)
    result = mem.compose(
        business_id=business.id,
        founder_id=founder.id,
        query=None,
        platform=ThreadPlatform.WEB,
    )
    # No summary, no FTS — should be the same as load_recent.
    assert [m.content for m in result] == ["one", "two", "three"]


def test_compose_dedupes_recent_window_against_fts(
    session: Session, business: Business, founder: Founder
) -> None:
    """Don't repeat a message twice if it matches FTS *and* is in recent window."""
    ensure_fts_index(session)
    _make_thread_with_messages(
        session,
        business,
        founder,
        ["pricing tiers question", "irrelevant", "another irrelevant one"],
    )
    mem = MemoryService(session=session, fts_top_k=5)
    result = mem.compose(
        business_id=business.id,
        founder_id=founder.id,
        query="pricing",
        platform=ThreadPlatform.WEB,
    )
    # The "pricing tiers question" should appear only once.
    occurrences = sum(1 for m in result if m.content == "pricing tiers question")
    assert occurrences == 1
