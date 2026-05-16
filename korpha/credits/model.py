"""CreditPool + CreditLedger — wallet model for action allowance."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Optional
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import primary_key_field, timestamp_field


class CreditLedgerKind(StrEnum):
    """How a ledger entry changed the balance."""

    GRANT = "grant"
    """Monthly allowance refill (or initial grant on pool creation)."""

    TOPUP = "topup"
    """User purchased additional credits — external billing reference
    stored in ``reference``."""

    DEBIT = "debit"
    """Credits consumed by an action."""

    REFUND = "refund"
    """Credits returned (e.g. failed action rollback)."""

    EXPIRE = "expire"
    """Credits removed because they aged out (if expiration is enabled
    for this pool — currently not exposed but reserved)."""

    ADJUST = "adjust"
    """Manual operator correction (positive or negative)."""


class CreditPool(SQLModel, table=True):
    """One wallet per business. At most one row per business_id."""

    __tablename__ = "credit_pool"

    id: UUID = primary_key_field()
    business_id: UUID = Field(
        foreign_key="business.id", index=True, unique=True,
    )

    balance: int = Field(
        default=0,
        description=(
            "Current spendable credits. Decrements on debit, "
            "increments on grant / topup / refund. Never negative — "
            "deduct() raises InsufficientCreditsError before going "
            "below zero."
        ),
    )

    monthly_grant: int = Field(
        default=0,
        description=(
            "Recurring monthly allowance. Applied on the "
            "next_refill_at rollover. Set to 0 for pay-as-you-go "
            "pools that only get credits via topup."
        ),
    )

    next_refill_at: datetime | None = Field(
        default=None,
        description=(
            "When the next monthly_grant will be applied. NULL means "
            "no automatic grant scheduled (call grant_monthly_if_due "
            "to bootstrap, or set explicitly to anchor the cadence)."
        ),
    )

    # Lifetime accounting for product analytics + finance reports.
    # Never decremented — these are running totals.
    lifetime_granted: int = Field(default=0)
    lifetime_purchased: int = Field(default=0)
    lifetime_debited: int = Field(default=0)

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


class CreditLedger(SQLModel, table=True):
    """Append-only audit row for every credit movement."""

    __tablename__ = "credit_ledger"

    id: UUID = primary_key_field()
    pool_id: UUID = Field(foreign_key="credit_pool.id", index=True)
    business_id: UUID = Field(foreign_key="business.id", index=True)

    kind: CreditLedgerKind = Field(index=True)
    amount: int = Field(
        description=(
            "Positive or negative integer. The sum of all amounts for "
            "a given pool always equals the pool's current balance "
            "(modulo float-safe accounting)."
        ),
    )
    balance_after: int = Field(
        description=(
            "Snapshot of pool.balance immediately after this entry "
            "was applied. Lets you reconstruct historical balance "
            "without replaying the whole ledger."
        ),
    )

    reference: str | None = Field(
        default=None,
        description=(
            "External reference: stripe_charge_id for TOPUP, "
            "action_id / skill_call_id for DEBIT, manual note for "
            "ADJUST. Free-form."
        ),
    )
    note: str | None = Field(default=None)

    created_at: datetime = timestamp_field(index=True)


__all__ = [
    "CreditLedger",
    "CreditLedgerKind",
    "CreditPool",
]
