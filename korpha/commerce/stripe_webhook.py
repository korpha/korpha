"""Stripe webhook handler — verifies signature + persists revenue events.

Stripe signs every webhook with HMAC-SHA256 using the endpoint's
``whsec_...`` secret. We verify before doing anything else; an
unsigned or wrongly-signed request is dropped at 400 with no DB
writes. Replay protection: we reject events older than 5 minutes
unless ``STRIPE_WEBHOOK_TOLERANCE`` env var widens the window.

Supported events:
  * ``checkout.session.completed`` — one-shot payment
  * ``invoice.paid`` — subscription period billed
  * ``charge.refunded`` — money flows back

Every event is persisted as a ``RevenueEvent`` row, idempotent
on Stripe's event id so retries don't double-count.

The webhook does not require API auth (Stripe is the auth, via
the signature). It DOES require ``STRIPE_WEBHOOK_SECRET`` to be
set — without it, every call returns 503 with 'webhook not
configured' so misconfiguration is loud.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.commerce.revenue import (
    RevenueEvent, RevenueKind, RevenueService,
)

logger = logging.getLogger(__name__)


_DEFAULT_TOLERANCE_SECONDS = 300  # 5 minutes; Stripe's recommendation


class StripeWebhookError(Exception):
    """Webhook processing failed in a way the caller should
    surface to Stripe (4xx/5xx)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__(message)


def _tolerance_seconds() -> int:
    raw = os.environ.get("STRIPE_WEBHOOK_TOLERANCE", "")
    try:
        return int(raw) if raw else _DEFAULT_TOLERANCE_SECONDS
    except ValueError:
        return _DEFAULT_TOLERANCE_SECONDS


def verify_signature(
    *,
    payload: bytes,
    signature_header: str | None,
    secret: str,
    tolerance_seconds: int | None = None,
) -> int:
    """Verify Stripe's ``Stripe-Signature`` header. Returns the
    timestamp on success; raises StripeWebhookError on any failure.

    Stripe's header is ``t=<timestamp>,v1=<sig1>,v1=<sig2>,...``
    (multiple signatures during key rotation). Any v1 sig matching
    HMAC-SHA256(secret, "<t>.<payload>") is accepted.
    """
    if not signature_header:
        raise StripeWebhookError(
            "missing Stripe-Signature header",
            status_code=400,
        )
    if not secret:
        raise StripeWebhookError(
            "STRIPE_WEBHOOK_SECRET not configured",
            status_code=503,
        )

    parts: dict[str, list[str]] = {}
    for chunk in signature_header.split(","):
        if "=" not in chunk:
            continue
        key, _, val = chunk.strip().partition("=")
        parts.setdefault(key.strip(), []).append(val.strip())

    timestamp_raw = (parts.get("t") or [None])[0]
    if not timestamp_raw:
        raise StripeWebhookError("malformed signature header (no t=)")
    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise StripeWebhookError(
            "malformed signature header (bad t=)",
        ) from exc

    tolerance = (
        tolerance_seconds
        if tolerance_seconds is not None
        else _tolerance_seconds()
    )
    age = time.time() - timestamp
    if abs(age) > tolerance:
        raise StripeWebhookError(
            f"signature timestamp outside tolerance "
            f"({int(age)}s vs {tolerance}s allowed); "
            "possible replay",
            status_code=400,
        )

    sigs = parts.get("v1") or []
    if not sigs:
        raise StripeWebhookError(
            "no v1 signature in header",
            status_code=400,
        )

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    for s in sigs:
        if hmac.compare_digest(expected, s):
            return timestamp
    raise StripeWebhookError(
        "signature verification failed",
        status_code=400,
    )


def _resolve_business(
    session: Session, event: dict,
) -> Business | None:
    """Find the Business this event belongs to.

    Resolution order:
      1. ``client_reference_id`` (set on checkout.session) treated
         as a business UUID.
      2. ``metadata.business_id`` on the object.
      3. The single Business in the DB (single-tenant fallback).

    Returns None when none of the above resolves — the caller
    persists nothing + returns 200 (so Stripe stops retrying)
    with a logged warning. Better than 5xx-looping forever on a
    misconfigured event we can't attribute."""
    obj = event.get("data", {}).get("object", {}) or {}

    raw_ref = obj.get("client_reference_id") or ""
    if raw_ref:
        try:
            return session.get(Business, UUID(str(raw_ref)))
        except (ValueError, AttributeError):
            pass

    meta = obj.get("metadata") or {}
    raw_meta = meta.get("business_id") or meta.get("korpha_business_id")
    if raw_meta:
        try:
            return session.get(Business, UUID(str(raw_meta)))
        except (ValueError, AttributeError):
            pass

    rows = list(session.exec(select(Business)).all())
    if len(rows) == 1:
        return rows[0]
    return None


def _extract_amount_usd(obj: dict) -> Decimal:
    """Pull ``amount_total`` (cents) and convert to USD decimal.

    Stripe returns amounts in the smallest currency unit (cents
    for USD). For non-USD currencies the value is still the
    smallest unit — we treat all of them as USD for now since
    that's the only currency korpha supports. If currency
    diversity becomes real, extend to per-currency tracking."""
    raw = obj.get("amount_total") or obj.get("amount_paid") or obj.get("amount", 0)
    try:
        cents = int(raw or 0)
    except (TypeError, ValueError):
        cents = 0
    return (Decimal(cents) / Decimal("100")).quantize(Decimal("0.01"))


@dataclass
class WebhookOutcome:
    """Returned from process_webhook for the caller (HTTP layer)
    to convert into a response."""

    status_code: int
    """200 on accept (even when we discarded the event because
    it's a duplicate or kind we don't care about). 4xx/5xx only
    on signature/configuration errors."""

    event_kind: str
    persisted: bool
    """True when a new RevenueEvent row was written. Duplicates
    + ignored event types return persisted=False."""

    revenue_event_id: UUID | None = None
    note: str = ""


def process_webhook(
    *,
    session: Session,
    payload: bytes,
    signature_header: str | None,
    secret: str | None = None,
) -> WebhookOutcome:
    """Verify + parse + persist. Caller handles the HTTP shape."""
    secret = secret if secret is not None else os.environ.get(
        "STRIPE_WEBHOOK_SECRET", "",
    )

    try:
        verify_signature(
            payload=payload, signature_header=signature_header,
            secret=secret,
        )
    except StripeWebhookError:
        # Re-raise so the HTTP layer returns the right status.
        raise

    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StripeWebhookError(
            f"webhook body is not valid JSON: {exc}",
            status_code=400,
        ) from exc

    event_kind = str(event.get("type") or "")
    event_id = str(event.get("id") or "")
    if not event_id:
        raise StripeWebhookError(
            "webhook event missing id", status_code=400,
        )

    obj = event.get("data", {}).get("object", {}) or {}
    occurred_unix = (
        event.get("created") or obj.get("created") or int(time.time())
    )
    occurred_at = datetime.fromtimestamp(
        int(occurred_unix), tz=timezone.utc,
    )

    business = _resolve_business(session, event)
    if business is None:
        logger.warning(
            "stripe webhook %s: could not resolve business; dropping",
            event_kind,
        )
        return WebhookOutcome(
            status_code=200, event_kind=event_kind, persisted=False,
            note="business_not_resolved",
        )

    # Map Stripe event types to RevenueKind
    kind: RevenueKind | None
    if event_kind == "checkout.session.completed":
        kind = RevenueKind.ONE_TIME
    elif event_kind == "invoice.paid":
        kind = RevenueKind.SUBSCRIPTION
    elif event_kind == "charge.refunded":
        kind = RevenueKind.REFUND
    else:
        # Unknown event types are not errors — Stripe sends many.
        # We just don't persist them.
        return WebhookOutcome(
            status_code=200, event_kind=event_kind, persisted=False,
            note="event_type_not_tracked",
        )

    amount_usd = _extract_amount_usd(obj)
    if event_kind == "charge.refunded":
        # ``charge.refunded`` carries amount_refunded
        try:
            cents = int(obj.get("amount_refunded") or 0)
        except (TypeError, ValueError):
            cents = 0
        amount_usd = (Decimal(cents) / Decimal("100")).quantize(
            Decimal("0.01"),
        )

    customer_email = (
        obj.get("customer_email")
        or (obj.get("customer_details") or {}).get("email")
    )
    customer_external_id = obj.get("customer")
    description = obj.get("description") or (
        f"Stripe {event_kind}"
    )

    rev_service = RevenueService(session)
    event_row, created = rev_service.record(
        business_id=business.id,
        provider="stripe",
        external_id=event_id,
        amount_usd=amount_usd,
        occurred_at=occurred_at,
        kind=kind,
        customer_email=customer_email,
        customer_external_id=customer_external_id,
        description=description,
    )
    return WebhookOutcome(
        status_code=200,
        event_kind=event_kind,
        persisted=created,
        revenue_event_id=event_row.id,
        note="ok" if created else "duplicate",
    )


__all__ = [
    "StripeWebhookError",
    "WebhookOutcome",
    "process_webhook",
    "verify_signature",
]
