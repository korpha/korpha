"""Shared side-effect dispatch for approved Approvals.

The CLI ``korpha execute <id>`` and the HTTP ``/approvals/{id}/approve``
both need to do the same thing once an approval is approved: actually
run the side effect (send the email, mint the Stripe link, write the
.ics, etc.). Before this module existed those paths drifted — the CLI
knew how to call Stripe but the HTTP path ran an empty CEO plan, so
Mike could approve a payment link in the dashboard and nothing
happened.

Each dispatch function:
- Takes ``(session, approval, payload, business)``.
- Returns ``DispatchResult(status, message, details)`` — no typer
  imports, no ``raise typer.Exit`` — pure logic.
- Persists what it did to ``approval.action_payload`` + an ``Activity``
  row so the dashboard can render the outcome without spelunking.

Callers translate ``DispatchResult`` into their surface (typer echo,
HTTP JSON, TUI toast). Failures DO NOT raise — they return a result
with ``ok=False``; the caller decides what to do with that.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from korpha.approvals.model import Approval
from korpha.audit.model import Activity, ActorType
from korpha.business.model import Business
from korpha.db._base import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of dispatching one approval's side effect.

    ``ok=True`` and ``ok=False`` are both successful returns — the
    caller must check before treating the message as a success.
    """
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _activity(
    session: Session,
    business: Business,
    approval: Approval,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Append an Activity row attributed to the unit + agent that
    owned the approval. Keeps weekly review / audit in sync with
    side effects."""
    session.add(
        Activity(
            business_id=business.id,
            business_unit_id=approval.business_unit_id,
            actor_type=ActorType.AGENT,
            actor_id=approval.agent_role_id,
            event_type=event_type,
            payload=payload,
        )
    )


async def dispatch_email_outreach(
    session: Session,
    approval: Approval,
    payload: dict[str, Any],
    business: Business,
) -> DispatchResult:
    """Send an approved EMAIL_OUTREACH approval through the
    configured Resend notifier. The skill that proposed it left
    {to, subject, body, from_address} in the payload."""
    from korpha.notifications import (
        Notification,
        NotifierError,
        ResendEmailNotifier,
    )

    to = str(payload.get("to") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    body = str(payload.get("body") or "").strip()
    from_addr = payload.get("from_address")
    from_addr_str = str(from_addr).strip() if from_addr else None

    if not (to and subject and body):
        return DispatchResult(
            ok=False,
            message="approval payload missing to/subject/body — refusing to send",
        )

    notifier = ResendEmailNotifier()
    try:
        await notifier.send(
            Notification(
                to=to,
                subject=subject,
                text_body=body,
                from_address=from_addr_str,
            )
        )
    except NotifierError as exc:
        approval.action_payload = {
            **(approval.action_payload or {}),
            "send_error": str(exc),
        }
        session.add(approval)
        _activity(
            session, business, approval,
            "email.send_failed",
            {"approval_id": str(approval.id), "error": str(exc)},
        )
        session.commit()
        return DispatchResult(
            ok=False,
            message=f"email send failed: {exc}",
            details={"error": str(exc)},
        )
    finally:
        await notifier.close()

    approval.action_payload = {
        **(approval.action_payload or {}),
        "sent_at": utcnow().isoformat(),
    }
    session.add(approval)
    _activity(
        session, business, approval,
        "email.sent",
        {
            "approval_id": str(approval.id),
            "to": to,
            "subject": subject,
        },
    )
    session.commit()
    return DispatchResult(
        ok=True,
        message=f"email sent to {to}",
        details={"to": to, "subject": subject},
    )


async def dispatch_commerce(
    session: Session,
    approval: Approval,
    payload: dict[str, Any],
    business: Business,
) -> DispatchResult:
    """Run an approved COMMERCE approval. Today the only kind is
    create_payment_link → mint a real Stripe Payment Link."""
    from korpha.commerce import StripeClient, StripeError

    api_key = os.getenv("STRIPE_API_KEY")
    if not api_key:
        return DispatchResult(
            ok=False,
            message=(
                "STRIPE_API_KEY not set. Add it via Settings → Credentials "
                "(or .env) before approving payment links."
            ),
        )

    kind = str(payload.get("kind") or "create_payment_link")
    if kind != "create_payment_link":
        return DispatchResult(
            ok=False,
            message=f"unsupported commerce kind {kind!r}",
        )

    name = str(payload.get("name") or "").strip()
    try:
        amount_usd = float(payload.get("amount_usd") or 0)
    except (TypeError, ValueError):
        amount_usd = 0.0
    currency = str(payload.get("currency") or "usd")
    description = payload.get("description")
    description_str = str(description).strip() if description else None

    if not name or amount_usd <= 0:
        return DispatchResult(
            ok=False,
            message="approval payload missing name / amount — refusing",
        )

    client = StripeClient(api_key=api_key)
    try:
        link = await client.create_payment_link(
            name=name,
            amount_usd=amount_usd,
            currency=currency,
            description=description_str,
        )
    except StripeError as exc:
        approval.action_payload = {
            **(approval.action_payload or {}),
            "execute_error": str(exc),
        }
        session.add(approval)
        _activity(
            session, business, approval,
            "commerce.payment_link_failed",
            {"approval_id": str(approval.id), "error": str(exc)},
        )
        session.commit()
        return DispatchResult(
            ok=False,
            message=f"stripe error: {exc}",
            details={"error": str(exc)},
        )
    finally:
        await client.close()

    approval.action_payload = {
        **(approval.action_payload or {}),
        "stripe_payment_link_id": link.id,
        "stripe_payment_link_url": link.url,
        "stripe_product_id": link.product_id,
        "executed_at": utcnow().isoformat(),
    }
    session.add(approval)
    _activity(
        session, business, approval,
        "commerce.payment_link_created",
        {
            "approval_id": str(approval.id),
            "stripe_payment_link_id": link.id,
            "stripe_payment_link_url": link.url,
        },
    )
    session.commit()
    return DispatchResult(
        ok=True,
        message=f"Stripe payment link live: {link.url}",
        details={"url": link.url, "id": link.id},
    )


async def dispatch_by_action_class(
    session: Session,
    approval: Approval,
    business: Business,
) -> DispatchResult | None:
    """Top-level router. Returns None when no side effect applies
    (e.g. PUBLIC_POST / INTERNAL kinds that are review-only).

    The author_* / create_cron kinds are still dispatched by the
    HTTP approve handler directly because they touch the in-process
    skill registry — not appropriate to call from the CLI executor.
    """
    from korpha.approvals.model import ActionClass

    payload = approval.action_payload or {}
    if approval.action_class == ActionClass.EMAIL_OUTREACH:
        return await dispatch_email_outreach(session, approval, payload, business)
    if approval.action_class == ActionClass.COMMERCE:
        return await dispatch_commerce(session, approval, payload, business)
    # PUBLIC_POST (landing copy), INTERNAL (reality check / kickoff
    # invite), CODE_CHANGE (handled separately by author_* dispatch in
    # the HTTP layer) — no automatic side effect, return None so the
    # caller can render "Approved (no further action)".
    return None
