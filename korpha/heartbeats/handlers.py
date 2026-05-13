"""Built-in handlers wired into the heartbeat dispatcher.

Call ``register_builtins()`` once at process start to make the standard
wakeup kinds available. Custom handlers can be added at any time via
``register_handler(kind, fn)``.
"""
from __future__ import annotations

import logging

from sqlmodel import select

from korpha.approvals.gate import ApprovalGate
from korpha.blockers.queue import BlockerQueue
from korpha.business.model import Business
from korpha.cofounder.chief_of_staff import ChiefOfStaff
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import Message as DbMessage
from korpha.cofounder.model import (
    MessageSenderType,
    Thread,
    ThreadPlatform,
    ThreadStatus,
)
from korpha.heartbeats.dispatcher import HandlerContext, register_handler
from korpha.heartbeats.model import WakeupKind
from korpha.identity.model import Founder

logger = logging.getLogger(__name__)


async def email_daily_digest(ctx: HandlerContext) -> None:
    """Build the morning digest snapshot and email it to the founder.

    Configured via wakeup payload:

      {"to": "mike@example.com"}        — explicit address (preferred)

    Falls back to the founder's email on file when 'to' is unset.
    Skips silently when no Resend key is configured (so a stale routine
    doesn't spam logs forever)."""
    import os

    if not os.getenv("RESEND_API_KEY"):
        logger.info("email.daily_digest skipped: RESEND_API_KEY not set")
        return

    from korpha.notifications import ResendEmailNotifier
    from korpha.notifications.digest import build_snapshot, render_digest

    session = ctx.session
    business = session.exec(
        select(Business).where(Business.id == ctx.wakeup.business_id)
    ).first()
    if business is None:
        return
    founder = session.exec(
        select(Founder).where(Founder.id == business.founder_id)
    ).first()
    if founder is None:
        return

    payload = ctx.wakeup.payload or {}
    to_addr = str(payload.get("to") or founder.email)
    if not to_addr:
        return

    snap = build_snapshot(session, business)
    notification = render_digest(snap, founder_name=founder.display_name)
    notification = type(notification)(  # rebuild with the resolved 'to'
        to=to_addr,
        subject=notification.subject,
        text_body=notification.text_body,
        html_body=notification.html_body,
        from_address=notification.from_address,
    )
    notifier = ResendEmailNotifier()
    try:
        await notifier.send(notification)
    finally:
        await notifier.close()


async def ceo_daily_digest(ctx: HandlerContext) -> None:
    """Generate the CoS digest, persist it as a Message in the CEO thread.

    The Founder sees the digest the next time they open the chat — no push
    yet (channels come later). This handler is intentionally side-effect
    light: it only reads existing blockers and writes one Message row.
    """
    session = ctx.session
    business_id = ctx.wakeup.business_id

    hiring = HiringService(session)
    queue = BlockerQueue(session=session)
    gate = ApprovalGate(session)
    cos = ChiefOfStaff(session=session, queue=queue, hiring=hiring, gate=gate)
    digest = cos.digest_for_ceo(business_id)
    if not digest.items:
        return  # nothing to surface — silent

    ceo = hiring.ensure_ceo(business_id)
    thread = session.exec(
        select(Thread)
        .where(Thread.business_id == business_id)
        .where(Thread.agent_role_id == ceo.id)
        .where(Thread.platform == ThreadPlatform.WEB)
        .where(Thread.status == ThreadStatus.ACTIVE)
    ).first()
    if thread is None:
        return  # no active web thread to post into

    msg = DbMessage(
        thread_id=thread.id,
        sender_type=MessageSenderType.AGENT,
        sender_role_id=ceo.id,
        content=digest.render(),
    )
    session.add(msg)
    session.commit()


_BUILTINS_REGISTERED = False


def register_builtins() -> None:
    """Idempotent registration of the standard handlers."""
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    register_handler(WakeupKind.CEO_DAILY_DIGEST.value, ceo_daily_digest)
    register_handler("email.daily_digest", email_daily_digest)
    _BUILTINS_REGISTERED = True


__all__ = ["ceo_daily_digest", "email_daily_digest", "register_builtins"]
