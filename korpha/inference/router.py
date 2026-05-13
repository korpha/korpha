"""InferenceRouter: picks a ProviderAccount for a request.

Implements two scheduling rules from ARCHITECTURE.md:

- **Session affinity**: same session_key → same account when possible
  (preserves prompt-cache hits, 50-90% cost reduction on cached prefix).
- **Cross-session distribution**: different session_keys distribute across
  healthy accounts to maximize parallelism.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from uuid import UUID

from korpha.audit.model import InferenceTier
from korpha.db._base import utcnow
from korpha.inference.registry import (
    AccountStatus,
    ProviderAccount,
    ProviderRegistry,
)


def next_quota_reset(
    quota: dict, *, now: datetime | None = None,
) -> datetime:
    """Return the next UTC datetime at which the free-tier quota
    resets. Used to set ``rate_limit_until`` on free-tier accounts
    after a 429 — instead of trusting retry_after (which OpenRouter
    sometimes omits or sets to a tiny value) we wait for the actual
    daily/hourly boundary."""
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    kind = quota.get("window_kind", "daily")
    reset_utc = str(quota.get("reset_utc") or "00:00")
    try:
        hh, mm = reset_utc.split(":")
        target_time = dt_time(int(hh), int(mm), tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        target_time = dt_time(0, 0, tzinfo=timezone.utc)

    if kind == "hourly":
        next_boundary = (now + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0,
        )
        return next_boundary
    if kind == "monthly":
        # First of next month at reset_utc.
        if now.month == 12:
            return datetime(
                now.year + 1, 1, 1, target_time.hour, target_time.minute,
                tzinfo=timezone.utc,
            )
        return datetime(
            now.year, now.month + 1, 1, target_time.hour, target_time.minute,
            tzinfo=timezone.utc,
        )
    # daily (default)
    today_reset = now.replace(
        hour=target_time.hour, minute=target_time.minute,
        second=0, microsecond=0,
    )
    if today_reset <= now:
        return today_reset + timedelta(days=1)
    return today_reset


class RoutingError(Exception):
    """No healthy account is available to serve the request."""


@dataclass
class _AccountState:
    in_flight: int = 0
    total_dispatched: int = 0


@dataclass
class InferenceRouter:
    """Stateful account picker. Thread-safe under a single asyncio loop."""

    registry: ProviderRegistry
    _affinity: dict[str, UUID] = field(default_factory=dict)
    _state: dict[UUID, _AccountState] = field(
        default_factory=lambda: defaultdict(_AccountState)
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def pick(
        self,
        tier: InferenceTier,
        session_key: str,
        *,
        pinned_account_label: str | None = None,
        exclude_ids: frozenset[UUID] | None = None,
    ) -> ProviderAccount:
        """Pick an account for the request. ``exclude_ids`` are accounts
        the caller has already tried in this dispatch — used by the pool
        to force cascade-forward progress when a transient ProviderError
        didn't trip the global rate-limit flag (so the account is still
        'healthy' from the router's perspective but we know it's failing
        this particular request)."""
        with self._lock:
            account = self._pick_locked(
                tier, session_key, pinned_label=pinned_account_label,
                exclude_ids=exclude_ids or frozenset(),
            )
            self._affinity[session_key] = account.id
            self._state[account.id].in_flight += 1
            self._state[account.id].total_dispatched += 1
            return account

    def release(self, account_id: UUID) -> None:
        with self._lock:
            state = self._state[account_id]
            if state.in_flight > 0:
                state.in_flight -= 1

    def mark_rate_limited(
        self,
        account_id: UUID,
        retry_after_seconds: float,
        session_key: str | None = None,
    ) -> None:
        """Mark account temporarily unhealthy and detach affinity for any
        session that was pinned to it.

        Free-tier accounts (those with ``free_tier_quota`` configured)
        ignore ``retry_after_seconds`` and instead wait until the next
        quota reset boundary — OpenRouter sometimes returns a tiny
        retry_after on free-tier 429 even though the daily cap is gone.
        Trusting it would burn another request immediately and re-trip
        the limit, looping forever."""
        now = utcnow()
        with self._lock:
            for account in self.registry.accounts():
                if account.id == account_id:
                    account.status = AccountStatus.RATE_LIMITED
                    if isinstance(account.free_tier_quota, dict):
                        account.rate_limit_until = next_quota_reset(
                            account.free_tier_quota, now=now,
                        )
                    else:
                        account.rate_limit_until = (
                            now + timedelta(seconds=retry_after_seconds)
                        )
                    break
            stale_keys = [k for k, aid in self._affinity.items() if aid == account_id]
            for k in stale_keys:
                del self._affinity[k]
            if (
                session_key is not None
                and self._affinity.get(session_key) == account_id
            ):
                del self._affinity[session_key]

    def reset_account_status(self, account_id: UUID) -> None:
        with self._lock:
            for account in self.registry.accounts():
                if account.id == account_id:
                    account.status = AccountStatus.ACTIVE
                    account.rate_limit_until = None
                    break

    def in_flight(self, account_id: UUID) -> int:
        return self._state[account_id].in_flight

    def total_dispatched(self, account_id: UUID) -> int:
        return self._state[account_id].total_dispatched

    def _pick_locked(
        self,
        tier: InferenceTier,
        session_key: str,
        *,
        pinned_label: str | None = None,
        exclude_ids: frozenset[UUID] = frozenset(),
    ) -> ProviderAccount:
        candidates = [
            c for c in self.registry.healthy_accounts_for_tier(tier)
            if c.id not in exclude_ids
        ]
        if not candidates:
            raise RoutingError(
                f"No healthy account serves tier {tier!s}. "
                f"Check account configuration, rate limits, and spend caps."
            )

        # Explicit account-label pin wins over session affinity. Used by
        # routines / heartbeats that want a specific provider for one
        # call (e.g. nightly summarizer pinned to a cheap workhorse so
        # subscription quota isn't burned on background work).
        if pinned_label:
            for c in candidates:
                if c.label == pinned_label:
                    return c
            # Pinned label didn't match a healthy account in this tier.
            # Fall through to normal routing — the call still succeeds
            # rather than failing on a stale label.
            import logging

            logging.getLogger(__name__).warning(
                "pinned_account_label=%r not found among healthy accounts for tier %s; "
                "falling back to normal routing",
                pinned_label,
                tier,
            )

        # Cascade ordering: pick from the lowest priority group that has
        # at least one healthy account with capacity. Within a priority
        # group, load-balance least-loaded (lets 13 OpenRouter free keys
        # all sit at priority=4 and round-robin fairly).
        with_capacity = [
            c for c in candidates if self._state[c.id].in_flight < c.concurrency_limit
        ]

        # Affinity only honored if the pinned account is in the lowest
        # available priority group. Once the primary tier recovers we
        # want sessions to migrate back, not stay stuck on a fallback.
        if with_capacity:
            best_priority = min(c.priority for c in with_capacity)
            top_tier = [c for c in with_capacity if c.priority == best_priority]
            pinned_id = self._affinity.get(session_key)
            if pinned_id is not None:
                for c in top_tier:
                    if c.id == pinned_id:
                        return c
            return min(
                top_tier,
                key=lambda c: (self._state[c.id].in_flight, self._state[c.id].total_dispatched),
            )

        # All healthy accounts at concurrency cap; still respect priority.
        best_priority = min(c.priority for c in candidates)
        top_tier = [c for c in candidates if c.priority == best_priority]
        return min(top_tier, key=lambda c: self._state[c.id].in_flight)
