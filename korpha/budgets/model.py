"""BudgetPolicy SQLModel — durable spend caps."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Optional
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import primary_key_field, timestamp_field


class BudgetScope(StrEnum):
    """What the cap applies to."""

    BUSINESS = "business"
    """Sum of every Cost row for the business."""

    BUSINESS_UNIT = "business_unit"
    """Sum of Costs whose business_unit_id matches the policy's
    business_unit_id field. Use to cap a specific Line (POD, KDP,
    Affiliate, etc.) without limiting siblings — exactly Paperclip's
    per-line cap pattern."""

    AGENT_ROLE = "agent_role"
    """Sum of Costs whose agent_role_id matches the policy's
    agent_role_id field. Use to cap a specific director / worker."""

    TIER = "tier"
    """Sum of Costs whose tier matches the policy's tier field.
    Use to cap Pro tier spend without limiting Workhorse."""


class BudgetWindow(StrEnum):
    """How long the rolling window is."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


_WINDOW_HOURS: dict[BudgetWindow, float] = {
    BudgetWindow.HOUR: 1.0,
    BudgetWindow.DAY: 24.0,
    BudgetWindow.WEEK: 24.0 * 7.0,
    BudgetWindow.MONTH: 24.0 * 30.0,
}


def window_hours(window: BudgetWindow) -> float:
    return _WINDOW_HOURS[window]


class BudgetPolicy(SQLModel, table=True):
    """One spend cap. Many policies can apply to a single Cost
    write (business cap + agent cap + tier cap); the strictest
    one trips first."""

    __tablename__ = "budget_policy"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    scope: BudgetScope = Field(index=True)

    # Optional scope qualifiers. Exactly one is non-null based on
    # ``scope``; the others stay null. We don't enforce that at
    # the DB level (would need a check constraint) — the service
    # layer validates on create.
    agent_role_id: Optional[UUID] = Field(
        default=None, foreign_key="agent_role.id", index=True,
    )
    business_unit_id: Optional[UUID] = Field(
        default=None, foreign_key="business_unit.id", index=True,
        description="When scope=business_unit: which Line/Unit this caps.",
    )
    tier: Optional[str] = Field(
        default=None, index=True,
        description="When scope=tier: 'workhorse' / 'pro' / etc.",
    )

    window: BudgetWindow = Field(default=BudgetWindow.DAY)
    limit_usd: Decimal = Field(
        max_digits=12, decimal_places=4,
        description="Spend cap in USD for the window.",
    )

    # Operational state
    is_active: bool = Field(default=True, index=True)
    """``False`` after a hard-stop trip (``paused`` semantically) or
    after the founder pauses manually. ``BudgetService.resume()``
    flips it back."""

    paused_reason: str | None = Field(default=None)
    """Why this policy was paused — ``hard_stop`` (auto, after
    the cap was exceeded) or ``manual`` (founder)."""

    paused_at: datetime | None = Field(default=None)

    last_window_start: datetime | None = Field(default=None)
    """Set when ``resume()`` is called so the next window starts
    fresh from that timestamp instead of inheriting any pre-pause
    spend."""

    label: str = Field(
        default="",
        description=(
            "Free-form human-readable label, e.g. 'CMO daily cap'. "
            "Shown on /app/disk + budget CLI."
        ),
    )

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


__all__ = [
    "BudgetPolicy",
    "BudgetScope",
    "BudgetWindow",
    "window_hours",
]
