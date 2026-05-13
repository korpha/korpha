"""ConversationRouter tests — sticky threads + single-voice rule."""
from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import ThreadPlatform
from korpha.cofounder.routing import (
    ConversationRouter,
    RoutingReason,
)
from korpha.db._base import utcnow
from korpha.identity.model import Founder


def _router(session: Session) -> ConversationRouter:
    return ConversationRouter(session=session, hiring=HiringService(session))


def test_inbound_default_routes_to_ceo(
    session: Session, business: Business, founder: Founder
) -> None:
    router = _router(session)
    decision = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="What should I do today?",
    )
    assert decision.reason == RoutingReason.DIRECT_TO_CEO


def test_force_agent_starts_sticky(
    session: Session, business: Business, founder: Founder, cmo: object
) -> None:
    router = _router(session)
    cmo_role = cmo  # type: ignore[assignment]
    decision = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="CMO, what's our content plan?",
        force_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    assert decision.reason == RoutingReason.STICKY_THREAD
    assert decision.delivering_agent_role_id == cmo_role.id  # type: ignore[attr-defined]
    assert router.is_sticky_active(decision.thread_id)


def test_sticky_thread_continues_default_routing(
    session: Session, business: Business, founder: Founder, cmo: object
) -> None:
    router = _router(session)
    cmo_role = cmo  # type: ignore[assignment]
    first = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="Initial CMO query",
        force_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    # Subsequent messages with no force → still go to CMO (sticky).
    follow = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="One more thing.",
    )
    assert follow.thread_id == first.thread_id
    assert follow.delivering_agent_role_id == cmo_role.id  # type: ignore[attr-defined]


def test_sticky_per_platform_isolated(
    session: Session, business: Business, founder: Founder, cmo: object
) -> None:
    """Sticky on Telegram does not affect Web routing."""
    router = _router(session)
    cmo_role = cmo  # type: ignore[assignment]
    router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.TELEGRAM,
        content="hi cmo",
        force_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    web = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="What's up",
    )
    assert web.reason == RoutingReason.DIRECT_TO_CEO


def test_close_thread_ends_sticky(
    session: Session, business: Business, founder: Founder, cmo: object
) -> None:
    router = _router(session)
    cmo_role = cmo  # type: ignore[assignment]
    decision = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="hi",
        force_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    assert router.is_sticky_active(decision.thread_id)
    router.close_thread(decision.thread_id)
    assert not router.is_sticky_active(decision.thread_id)


def test_outbound_csuite_routes_through_ceo(
    session: Session, business: Business, founder: Founder, cmo: object
) -> None:
    """CMO proactively pinging Founder gets relayed via CEO (single-voice)."""
    router = _router(session)
    cmo_role = cmo  # type: ignore[assignment]
    decision = router.route_outbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="Marketing budget needs Founder approval",
        requesting_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    assert decision.reason == RoutingReason.SINGLE_VOICE_RELAY
    assert decision.original_requester_role_id == cmo_role.id  # type: ignore[attr-defined]
    # Delivered by CEO, not CMO.
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    assert decision.delivering_agent_role_id == ceo.id


def test_outbound_inside_sticky_uses_csuite_voice(
    session: Session, business: Business, founder: Founder, cmo: object
) -> None:
    """If Founder is in a sticky CMO thread, CMO speaks in her own voice there."""
    router = _router(session)
    cmo_role = cmo  # type: ignore[assignment]
    sticky = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="hi cmo",
        force_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    out = router.route_outbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="Here's the plan",
        requesting_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    assert out.reason == RoutingReason.STICKY_THREAD
    assert out.delivering_agent_role_id == cmo_role.id  # type: ignore[attr-defined]
    assert out.thread_id == sticky.thread_id


def test_outbound_ceo_speaks_directly(
    session: Session, business: Business, founder: Founder
) -> None:
    router = _router(session)
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    decision = router.route_outbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="Daily summary",
        requesting_agent_role_id=ceo.id,
    )
    assert decision.reason == RoutingReason.DIRECT_TO_CEO
    assert decision.delivering_agent_role_id == ceo.id


def test_sticky_expires_after_ttl(
    session: Session, business: Business, founder: Founder, cmo: object
) -> None:
    """When sticky_until passes, sticky is no longer active and routing falls back to CEO."""
    base_now = utcnow()
    advance = {"delta": timedelta(seconds=0)}

    def fake_now() -> object:
        return base_now + advance["delta"]

    router = ConversationRouter(
        session=session,
        hiring=HiringService(session),
        sticky_ttl_seconds=60,
        _now_fn=fake_now,
    )
    cmo_role = cmo  # type: ignore[assignment]
    sticky = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="hi",
        force_agent_role_id=cmo_role.id,  # type: ignore[attr-defined]
    )
    assert router.is_sticky_active(sticky.thread_id)

    advance["delta"] = timedelta(seconds=120)
    assert not router.is_sticky_active(sticky.thread_id)

    follow = router.route_inbound(
        business_id=business.id,
        founder_id=founder.id,
        platform=ThreadPlatform.WEB,
        content="one more thing",
    )
    assert follow.reason == RoutingReason.DIRECT_TO_CEO
