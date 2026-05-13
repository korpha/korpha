"""Revenue tracking — real money flowing in.

Persists revenue events from Stripe webhooks (and any future
adapter — Lemon Squeezy, Paddle, ConvertKit Commerce) so
``finance.monthly_review`` can stop guessing from approval
payloads and report actual MRR.

One row per event. Idempotent on Stripe's ``id`` so webhook
retries don't double-count. Multi-tenant via ``business_id`` —
when an install runs multiple businesses through the same
Stripe account, the route resolves the right business by the
event's metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlmodel import Field, Session, SQLModel, select

from korpha.db._base import primary_key_field, timestamp_field


class RevenueKind(StrEnum):
    """What flavor of money is this?"""

    ONE_TIME = "one_time"
    """Single payment — payment link, one-shot checkout."""

    SUBSCRIPTION = "subscription"
    """Recurring charge — monthly / yearly. Tracked per-period
    so MRR math is straight."""

    REFUND = "refund"
    """Money flowing back out. Stored as a positive amount with
    kind=refund so reports can subtract cleanly."""


class RevenueEvent(SQLModel, table=True):
    """One revenue event. Append-only — refunds add a new row,
    they never mutate the original sale."""

    __tablename__ = "revenue_event"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    # Source attribution
    provider: str = Field(
        index=True,
        description=(
            "'stripe' / 'lemonsqueezy' / 'paddle' / 'manual'. "
            "Free-form so future adapters slot in without a schema "
            "migration."
        ),
    )
    external_id: str = Field(
        index=True,
        description=(
            "Provider's event id (Stripe's evt_... / cs_... / "
            "in_...). Used for idempotent dedup — a webhook retry "
            "with the same id is a no-op."
        ),
    )

    kind: RevenueKind = Field(default=RevenueKind.ONE_TIME, index=True)
    amount_usd: Decimal = Field(
        default=Decimal("0"), max_digits=12, decimal_places=2,
        description="Always positive. For refunds, kind=refund + amount=>0.",
    )

    customer_email: str | None = Field(default=None, index=True)
    customer_external_id: str | None = Field(
        default=None, index=True,
        description="Stripe customer id (cus_...) etc.",
    )
    description: str | None = Field(
        default=None,
        description=(
            "Human-readable label for the dashboard — product name "
            "or invoice number."
        ),
    )

    occurred_at: datetime = timestamp_field(index=True)
    """When the underlying event happened at the provider, NOT
    when we received the webhook. Used by all spend/revenue
    windowed queries."""

    received_at: datetime = timestamp_field()


@dataclass
class RevenueService:
    """Per-Session revenue ops."""

    session: Session

    def record(
        self, *,
        business_id: UUID,
        provider: str,
        external_id: str,
        amount_usd: Decimal,
        occurred_at: datetime,
        kind: RevenueKind = RevenueKind.ONE_TIME,
        customer_email: str | None = None,
        customer_external_id: str | None = None,
        description: str | None = None,
    ) -> tuple[RevenueEvent, bool]:
        """Idempotent record. Returns ``(event, created)``.
        ``created=False`` when the external_id was already in the
        DB — a webhook retry."""
        existing = self.session.exec(
            select(RevenueEvent)
            .where(RevenueEvent.provider == provider)
            .where(RevenueEvent.external_id == external_id)
        ).first()
        if existing is not None:
            return existing, False

        event = RevenueEvent(
            business_id=business_id,
            provider=provider,
            external_id=external_id,
            kind=kind,
            amount_usd=amount_usd,
            customer_email=customer_email,
            customer_external_id=customer_external_id,
            description=description,
            occurred_at=occurred_at,
        )
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event, True

    def total_in_window(
        self, *,
        business_id: UUID,
        start: datetime,
        end: datetime,
    ) -> Decimal:
        """Net revenue: sum of one-time + subscription minus
        refunds, scoped to ``business_id`` + occurred_at window."""
        rows = list(self.session.exec(
            select(RevenueEvent)
            .where(RevenueEvent.business_id == business_id)
            .where(RevenueEvent.occurred_at >= start)
            .where(RevenueEvent.occurred_at < end)
        ).all())
        net = Decimal("0")
        for r in rows:
            if r.kind == RevenueKind.REFUND:
                net -= r.amount_usd
            else:
                net += r.amount_usd
        return net

    def list_in_window(
        self, *,
        business_id: UUID,
        start: datetime,
        end: datetime,
    ) -> list[RevenueEvent]:
        return list(self.session.exec(
            select(RevenueEvent)
            .where(RevenueEvent.business_id == business_id)
            .where(RevenueEvent.occurred_at >= start)
            .where(RevenueEvent.occurred_at < end)
            .order_by(RevenueEvent.occurred_at.desc())  # type: ignore[attr-defined]
        ).all())


__all__ = [
    "RevenueEvent",
    "RevenueKind",
    "RevenueService",
]
