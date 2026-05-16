"""CreditService — deduct, grant, topup, balance.

All mutations write a :class:`CreditLedger` row so a full audit trail
exists alongside the wallet snapshot. Reads are cheap (single row by
business_id); writes are two rows (pool update + ledger insert).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from korpha.credits.model import (
    CreditLedger,
    CreditLedgerKind,
    CreditPool,
)

logger = logging.getLogger(__name__)


# How long a "month" is for refill cadence. Calendar months are a pain
# to do right across timezones; we use a 30-day window which is what
# most credit pricing pages actually mean by "monthly" anyway.
_MONTH_DAYS = 30


class InsufficientCreditsError(Exception):
    """Raised when a deduct() call would push balance below zero."""

    def __init__(self, *, balance: int, requested: int) -> None:
        self.balance = balance
        self.requested = requested
        super().__init__(
            f"insufficient credits: have {balance}, need {requested}"
        )


@dataclass
class CreditService:
    """Per-Session credit operations."""

    session: Session

    # ---- pool ops ----

    def get_pool(self, business_id: UUID) -> CreditPool | None:
        return self.session.exec(
            select(CreditPool).where(
                CreditPool.business_id == business_id,
            )
        ).first()

    def get_or_create_pool(
        self,
        business_id: UUID,
        *,
        monthly_grant: int = 0,
        initial_grant: int = 0,
    ) -> CreditPool:
        """Lookup or create. ``monthly_grant`` is the per-month cadence;
        ``initial_grant`` seeds the balance on creation (typically equal
        to monthly_grant for a fresh sign-up). Idempotent for existing
        pools — we never overwrite settings on an existing pool here,
        callers wanting to change cadence call ``update_pool``."""
        pool = self.get_pool(business_id)
        if pool is not None:
            return pool
        now = datetime.now(tz=timezone.utc)
        pool = CreditPool(
            business_id=business_id,
            balance=int(initial_grant),
            monthly_grant=int(monthly_grant),
            next_refill_at=now + timedelta(days=_MONTH_DAYS),
            lifetime_granted=int(initial_grant),
        )
        self.session.add(pool)
        self.session.commit()
        self.session.refresh(pool)
        if initial_grant > 0:
            self._record(
                pool, kind=CreditLedgerKind.GRANT,
                amount=initial_grant,
                note="initial grant on pool creation",
            )
        return pool

    def update_pool(
        self,
        business_id: UUID,
        *,
        monthly_grant: int | None = None,
    ) -> CreditPool:
        pool = self.get_pool(business_id)
        if pool is None:
            raise KeyError(f"no credit pool for business {business_id}")
        if monthly_grant is not None:
            pool.monthly_grant = int(monthly_grant)
        pool.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(pool)
        self.session.commit()
        self.session.refresh(pool)
        return pool

    # ---- balance changes ----

    def deduct(
        self,
        business_id: UUID,
        amount: int,
        *,
        reference: str | None = None,
        note: str | None = None,
    ) -> CreditPool:
        """Subtract ``amount`` from the pool. Raises
        :class:`InsufficientCreditsError` if it would go negative;
        does NOT auto-create the pool — callers must have set one up
        already (otherwise no pool means uncapped, which is the right
        default for self-hosted installs)."""
        if amount <= 0:
            raise ValueError(f"deduct amount must be > 0, got {amount}")
        pool = self.get_pool(business_id)
        if pool is None:
            # No pool = uncapped. Don't fail loudly — let the action
            # proceed. The wrapper layer can ensure a pool exists for
            # plans that care.
            return None  # type: ignore[return-value]
        if pool.balance < amount:
            raise InsufficientCreditsError(
                balance=pool.balance, requested=amount,
            )
        pool.balance -= amount
        pool.lifetime_debited += amount
        pool.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(pool)
        self.session.commit()
        self.session.refresh(pool)
        self._record(
            pool, kind=CreditLedgerKind.DEBIT,
            amount=-amount, reference=reference, note=note,
        )
        return pool

    def topup(
        self,
        business_id: UUID,
        amount: int,
        *,
        reference: str | None = None,
        note: str | None = None,
    ) -> CreditPool:
        """Add purchased credits to the pool. ``reference`` should be
        the external payment id (Stripe charge id, PayPal txn id, etc.)
        so the audit trail links back to the billing system."""
        if amount <= 0:
            raise ValueError(f"topup amount must be > 0, got {amount}")
        pool = self.get_or_create_pool(business_id)
        pool.balance += amount
        pool.lifetime_purchased += amount
        pool.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(pool)
        self.session.commit()
        self.session.refresh(pool)
        self._record(
            pool, kind=CreditLedgerKind.TOPUP,
            amount=amount, reference=reference, note=note,
        )
        return pool

    def grant_monthly_if_due(
        self,
        business_id: UUID,
        *,
        now: Optional[datetime] = None,
    ) -> CreditPool | None:
        """Apply the monthly grant if next_refill_at has passed.
        Idempotent — calling this every heartbeat tick is fine, the
        next_refill_at check ensures we don't double-grant.

        Returns the pool if a grant was applied, else None."""
        now = now or datetime.now(tz=timezone.utc)
        pool = self.get_pool(business_id)
        if pool is None or pool.monthly_grant <= 0:
            return None
        refill_at = pool.next_refill_at
        if refill_at is None:
            return None
        # Normalize naive datetimes to UTC-aware so comparison works
        # across SQLite (naive) + Postgres (aware) backends.
        if refill_at.tzinfo is None:
            refill_at = refill_at.replace(tzinfo=timezone.utc)
        if now < refill_at:
            return None
        pool.balance += pool.monthly_grant
        pool.lifetime_granted += pool.monthly_grant
        pool.next_refill_at = now + timedelta(days=_MONTH_DAYS)
        pool.updated_at = now
        self.session.add(pool)
        self.session.commit()
        self.session.refresh(pool)
        self._record(
            pool, kind=CreditLedgerKind.GRANT,
            amount=pool.monthly_grant,
            note=f"monthly refill, next at {pool.next_refill_at}",
        )
        return pool

    def adjust(
        self,
        business_id: UUID,
        amount: int,
        *,
        note: str = "manual adjustment",
    ) -> CreditPool:
        """Operator correction. Positive or negative. Floors at 0."""
        pool = self.get_or_create_pool(business_id)
        new_balance = max(0, pool.balance + amount)
        delta = new_balance - pool.balance
        pool.balance = new_balance
        pool.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(pool)
        self.session.commit()
        self.session.refresh(pool)
        self._record(
            pool, kind=CreditLedgerKind.ADJUST,
            amount=delta, note=note,
        )
        return pool

    # ---- history ----

    def recent_ledger(
        self,
        business_id: UUID,
        *,
        limit: int = 50,
    ) -> list[CreditLedger]:
        return list(self.session.exec(
            select(CreditLedger)
            .where(CreditLedger.business_id == business_id)
            .order_by(CreditLedger.created_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        ).all())

    # ---- internals ----

    def _record(
        self,
        pool: CreditPool,
        *,
        kind: CreditLedgerKind,
        amount: int,
        reference: str | None = None,
        note: str | None = None,
    ) -> CreditLedger:
        entry = CreditLedger(
            pool_id=pool.id,
            business_id=pool.business_id,
            kind=kind,
            amount=amount,
            balance_after=pool.balance,
            reference=reference,
            note=note,
        )
        self.session.add(entry)
        self.session.commit()
        self.session.refresh(entry)
        return entry


__all__ = [
    "CreditService",
    "InsufficientCreditsError",
]
