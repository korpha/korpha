"""BudgetService — check/enforce/track per-policy spend.

CostTracker.complete() calls ``check_before_complete()`` BEFORE
firing the LLM. If any active policy is over its cap, we raise
``BudgetExceededError`` synchronously — the caller's request
never goes out, no tokens are spent.

After the response lands (Cost row written), the same path calls
``maybe_pause_after_complete()`` which re-totals each policy and
flips it to ``is_active=False`` with reason=``hard_stop`` if it
just went over. The pause prevents the *next* call; the call
that triggered the trip has already completed (the founder ate
that one). Tradeoff: at most one over-cap call per trip vs. the
cost of fetching pre-totals around every LLM call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import Cost
from korpha.budgets.model import (
    BudgetPolicy,
    BudgetScope,
    BudgetWindow,
    window_hours,
)

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when an active policy is over its limit. Carries
    the policy id so caller surfaces can render context."""

    def __init__(
        self,
        *,
        policy_id: UUID,
        scope: BudgetScope,
        window: BudgetWindow,
        spent_usd: Decimal,
        limit_usd: Decimal,
        label: str = "",
    ) -> None:
        self.policy_id = policy_id
        self.scope = scope
        self.window = window
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.label = label
        super().__init__(
            f"budget exceeded: {label or scope.value} "
            f"({window.value}) — spent ${spent_usd:.4f} of "
            f"${limit_usd:.4f} cap"
        )


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of one policy at one moment."""

    policy: BudgetPolicy
    spent_usd: Decimal
    pct_used: float
    """0.0 to 1.0+. Over 1.0 means already exceeded."""

    is_paused: bool

    @property
    def remaining_usd(self) -> Decimal:
        return max(Decimal("0"), self.policy.limit_usd - self.spent_usd)


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; normalize to UTC-aware so
    comparisons + subtractions don't crash."""
    return dt if dt.tzinfo is not None else dt.replace(
        tzinfo=timezone.utc,
    )


def _window_start(
    policy: BudgetPolicy, *, now: datetime,
) -> datetime:
    """Compute the rolling-window start for this policy.

    If ``last_window_start`` is set (set by ``resume()``), the
    window is anchored there until it ages out — so a fresh
    resume gives the founder a clean slate. Otherwise the window
    is purely rolling: ``now - window_hours``."""
    now = _ensure_aware(now)
    width = timedelta(hours=window_hours(policy.window))
    if policy.last_window_start is not None:
        anchored = _ensure_aware(policy.last_window_start)
        anchored_end = anchored + width
        if anchored_end >= now:
            return anchored
    return now - width


def _spent_in_window(
    session: Session,
    policy: BudgetPolicy,
    *,
    now: Optional[datetime] = None,
) -> Decimal:
    """Sum Cost rows that count toward this policy in the
    current window."""
    now = now or datetime.now(tz=timezone.utc)
    start = _window_start(policy, now=now)

    stmt = (
        select(Cost)
        .where(Cost.business_id == policy.business_id)
        .where(Cost.created_at >= start)
    )
    if policy.scope == BudgetScope.AGENT_ROLE:
        if policy.agent_role_id is None:
            return Decimal("0")
        stmt = stmt.where(
            Cost.agent_role_id == policy.agent_role_id,
        )
    elif policy.scope == BudgetScope.BUSINESS_UNIT:
        if policy.business_unit_id is None:
            return Decimal("0")
        stmt = stmt.where(
            Cost.business_unit_id == policy.business_unit_id,
        )
    elif policy.scope == BudgetScope.TIER:
        if not policy.tier:
            return Decimal("0")
        stmt = stmt.where(Cost.tier == policy.tier)

    rows = list(session.exec(stmt).all())
    return sum((c.cost_usd for c in rows), Decimal("0"))


@dataclass
class BudgetService:
    """Per-Session budget operations."""

    session: Session

    # ---- create / read ----

    def create(
        self,
        *,
        business_id: UUID,
        scope: BudgetScope,
        limit_usd: Decimal,
        window: BudgetWindow = BudgetWindow.DAY,
        agent_role_id: Optional[UUID] = None,
        business_unit_id: Optional[UUID] = None,
        tier: Optional[str] = None,
        label: str = "",
    ) -> BudgetPolicy:
        if limit_usd <= 0:
            raise ValueError("budget: limit_usd must be > 0")
        if scope == BudgetScope.AGENT_ROLE and agent_role_id is None:
            raise ValueError(
                "budget: scope=agent_role requires agent_role_id",
            )
        if scope == BudgetScope.BUSINESS_UNIT and business_unit_id is None:
            raise ValueError(
                "budget: scope=business_unit requires business_unit_id",
            )
        if scope == BudgetScope.TIER and not tier:
            raise ValueError(
                "budget: scope=tier requires tier name",
            )
        if scope == BudgetScope.BUSINESS and (
            agent_role_id is not None or business_unit_id is not None or tier
        ):
            raise ValueError(
                "budget: scope=business takes neither "
                "agent_role_id, business_unit_id, nor tier",
            )
        policy = BudgetPolicy(
            business_id=business_id,
            scope=scope,
            agent_role_id=agent_role_id,
            business_unit_id=business_unit_id,
            tier=tier,
            window=window,
            limit_usd=Decimal(limit_usd),
            label=label,
        )
        self.session.add(policy)
        self.session.commit()
        self.session.refresh(policy)
        return policy

    def list_for_business(
        self,
        business_id: UUID,
        *,
        active_only: bool = False,
    ) -> list[BudgetPolicy]:
        stmt = select(BudgetPolicy).where(
            BudgetPolicy.business_id == business_id,
        )
        if active_only:
            stmt = stmt.where(BudgetPolicy.is_active)
        return list(self.session.exec(stmt).all())

    def get(self, policy_id: UUID) -> BudgetPolicy | None:
        return self.session.get(BudgetPolicy, policy_id)

    # ---- enforcement ----

    def check_before_complete(
        self,
        *,
        business_id: UUID,
        agent_role_id: Optional[UUID] = None,
        business_unit_id: Optional[UUID] = None,
        tier: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Raise BudgetExceededError if any active policy that
        applies to this call is already over its cap.

        ``agent_role_id`` + ``tier`` come from the in-flight
        request so we only consult policies that actually scope
        this call."""
        policies = self.list_for_business(business_id, active_only=True)
        for policy in policies:
            if not _applies(
                policy, agent_role_id=agent_role_id,
                business_unit_id=business_unit_id, tier=tier,
            ):
                continue
            spent = _spent_in_window(self.session, policy, now=now)
            if spent >= policy.limit_usd:
                self._auto_pause(
                    policy, reason="hard_stop",
                    spent=spent,
                )
                raise BudgetExceededError(
                    policy_id=policy.id,
                    scope=policy.scope,
                    window=policy.window,
                    spent_usd=spent,
                    limit_usd=policy.limit_usd,
                    label=policy.label,
                )

    def maybe_pause_after_complete(
        self,
        *,
        business_id: UUID,
        agent_role_id: Optional[UUID] = None,
        business_unit_id: Optional[UUID] = None,
        tier: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> list[BudgetPolicy]:
        """Re-check after a Cost was just written. Pause any
        policies that just crossed their limit. Returns the
        newly-paused policies."""
        paused: list[BudgetPolicy] = []
        for policy in self.list_for_business(
            business_id, active_only=True,
        ):
            if not _applies(
                policy, agent_role_id=agent_role_id,
                business_unit_id=business_unit_id, tier=tier,
            ):
                continue
            spent = _spent_in_window(self.session, policy, now=now)
            if spent >= policy.limit_usd:
                self._auto_pause(
                    policy, reason="hard_stop", spent=spent,
                )
                paused.append(policy)
        return paused

    def resume(
        self, policy_id: UUID, *, now: Optional[datetime] = None,
    ) -> BudgetPolicy:
        """Reactivate a paused policy. Sets ``last_window_start`` to
        ``now`` so the next window starts fresh — the founder's
        resume action means "ok, start counting from here." Without
        this anchor a paused-and-resumed policy would re-trip
        immediately because the rolling window still includes the
        pre-pause overage."""
        policy = self.session.get(BudgetPolicy, policy_id)
        if policy is None:
            raise KeyError(f"budget policy {policy_id} not found")
        policy.is_active = True
        policy.paused_reason = None
        policy.paused_at = None
        policy.last_window_start = now or datetime.now(tz=timezone.utc)
        policy.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(policy)
        self.session.commit()
        self.session.refresh(policy)
        return policy

    def pause(
        self, policy_id: UUID, *, reason: str = "manual",
    ) -> BudgetPolicy:
        policy = self.session.get(BudgetPolicy, policy_id)
        if policy is None:
            raise KeyError(f"budget policy {policy_id} not found")
        self._auto_pause(policy, reason=reason)
        return policy

    def delete(self, policy_id: UUID) -> bool:
        policy = self.session.get(BudgetPolicy, policy_id)
        if policy is None:
            return False
        self.session.delete(policy)
        self.session.commit()
        return True

    # ---- status ----

    def status(
        self,
        business_id: UUID,
        *,
        now: Optional[datetime] = None,
    ) -> list[BudgetStatus]:
        """All policies + current usage. Sorted by pct_used desc
        so the closest-to-trip lands first."""
        out: list[BudgetStatus] = []
        for policy in self.list_for_business(business_id):
            spent = _spent_in_window(
                self.session, policy, now=now,
            )
            limit = policy.limit_usd or Decimal("0.0001")
            pct = float(spent / limit) if limit > 0 else 0.0
            out.append(BudgetStatus(
                policy=policy,
                spent_usd=spent,
                pct_used=pct,
                is_paused=not policy.is_active,
            ))
        out.sort(key=lambda s: -s.pct_used)
        return out

    # ---- internals ----

    def _auto_pause(
        self,
        policy: BudgetPolicy,
        *,
        reason: str,
        spent: Optional[Decimal] = None,
    ) -> None:
        """Mark a policy paused. Idempotent."""
        if not policy.is_active and policy.paused_reason == reason:
            return
        policy.is_active = False
        policy.paused_reason = reason
        policy.paused_at = datetime.now(tz=timezone.utc)
        policy.updated_at = datetime.now(tz=timezone.utc)
        self.session.add(policy)
        self.session.commit()
        if reason == "hard_stop":
            logger.warning(
                "budget hard-stop: policy %s (%s) at $%.4f / $%.4f",
                policy.label or policy.scope.value,
                policy.window.value,
                float(spent or 0), float(policy.limit_usd),
            )


def _applies(
    policy: BudgetPolicy,
    *,
    agent_role_id: Optional[UUID],
    business_unit_id: Optional[UUID] = None,
    tier: Optional[str],
) -> bool:
    """Does this policy scope apply to a call with these
    coordinates? BUSINESS-scoped applies to every call;
    AGENT_ROLE only when role matches; BUSINESS_UNIT only when
    unit matches; TIER only when tier matches."""
    if policy.scope == BudgetScope.BUSINESS:
        return True
    if policy.scope == BudgetScope.AGENT_ROLE:
        return (
            agent_role_id is not None
            and agent_role_id == policy.agent_role_id
        )
    if policy.scope == BudgetScope.BUSINESS_UNIT:
        return (
            business_unit_id is not None
            and business_unit_id == policy.business_unit_id
        )
    if policy.scope == BudgetScope.TIER:
        return bool(tier) and tier == policy.tier
    return False


__all__ = [
    "BudgetExceededError",
    "BudgetService",
    "BudgetStatus",
]
