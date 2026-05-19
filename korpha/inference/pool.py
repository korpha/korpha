"""InferencePool: top-level entry point.

```
pool = InferencePool(providers=[mock], accounts=[acc1, acc2])
response = await pool.complete(request)
async for chunk in pool.stream(request):
    ...
```

Handles: routing, rate-limit retries with account swap, in-flight tracking,
and account spend updates.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta

from korpha.db._base import utcnow
from korpha.inference.provider import Provider, ProviderError, RateLimitError
from korpha.inference.registry import (
    AccountStatus, ProviderAccount, ProviderRegistry,
)
from korpha.inference.router import InferenceRouter, RoutingError
from korpha.inference.types import CompletionRequest, CompletionResponse, StreamChunk

logger = logging.getLogger(__name__)


# Default backoff schedule for the "whole pool exhausted" case.
# Wait 2s, 4s, 8s, 16s, 32s — total ~62s of patience before giving
# up on transient rate-limits. Accounts whose rate_limit_until is
# > 60s away (i.e. true daily quota) are excluded from the unlock
# pass so we don't burn through their per-day cap on retries.
_DEFAULT_POOL_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0)

# Transient rate-limit threshold: if an account's rate_limit_until is
# within this many seconds, we treat it as "OpenRouter org-wide
# transient throttle" rather than "daily quota exhausted" and let
# the exponential backoff unlock it on retry. Above this, the account
# is treated as truly exhausted (daily quota) and stays out.
_TRANSIENT_RATE_LIMIT_HORIZON_SECONDS = 60.0


@dataclass
class InferencePool:
    providers: list[Provider]
    accounts: list[ProviderAccount]
    max_swap_attempts: int = 0
    """How many account swaps to try per pool-backoff round. 0 = auto:
    use ``len(accounts)`` so a single round tries every account once.
    Set explicitly to cap (e.g. for tests). Each swap = one account."""

    pool_backoff_seconds: tuple[float, ...] = _DEFAULT_POOL_BACKOFF_SECONDS
    """When every account is rate-limited mid-call, sleep these
    intervals and re-try the whole pool. Catches the transient-429
    case (OpenRouter org throttle clears in seconds) without waiting
    for true daily-quota reset. Set to () to disable backoff."""

    registry: ProviderRegistry = field(init=False)
    router: InferenceRouter = field(init=False)

    def __post_init__(self) -> None:
        self.registry = ProviderRegistry()
        for p in self.providers:
            self.registry.register_provider(p)
        for a in self.accounts:
            self.registry.add_account(a)
        self.router = InferenceRouter(registry=self.registry)
        if self.max_swap_attempts <= 0:
            # Default = try every configured account once per round.
            # Min 3 so a 1-account install still gets a couple of retries.
            self.max_swap_attempts = max(3, len(self.accounts))

    def _unlock_transient_accounts(self) -> int:
        """Flip RATE_LIMITED accounts back to ACTIVE if their
        rate_limit_until is close enough that the throttle was
        plausibly transient (not daily-quota-exhausted).

        Returns count unlocked — caller uses 0 as "nothing to retry,
        stop backing off"."""
        now = utcnow()
        horizon = now + timedelta(seconds=_TRANSIENT_RATE_LIMIT_HORIZON_SECONDS)
        unlocked = 0
        for account in self.registry.accounts():
            if account.status != AccountStatus.RATE_LIMITED:
                continue
            if account.rate_limit_until is None:
                # No expiry recorded — assume transient.
                self.router.reset_account_status(account.id)
                unlocked += 1
                continue
            # Normalize naive datetimes the same way the router does.
            ru = account.rate_limit_until
            if ru.tzinfo is None:
                from datetime import timezone as _tz
                ru = ru.replace(tzinfo=_tz.utc)
            if ru <= horizon:
                self.router.reset_account_status(account.id)
                unlocked += 1
        return unlocked

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        # Outer loop: pool-level exponential backoff. The inner swap
        # loop tries every account once; if all of them 429 in the
        # same call (transient OpenRouter org throttle, all-free-keys-
        # exhausted, etc.), sleep + unlock-transient + retry the
        # whole pool. Tried set resets each round so previously-429'd
        # accounts get another shot.
        last_error: Exception | None = None
        for backoff_idx, _ in enumerate([0.0, *self.pool_backoff_seconds]):
            if backoff_idx > 0:
                wait = self.pool_backoff_seconds[backoff_idx - 1]
                unlocked = self._unlock_transient_accounts()
                if unlocked == 0:
                    # Nothing transient to retry — only daily-quota
                    # accounts remain. Backing off won't help.
                    break
                logger.info(
                    "inference.pool: all accounts rate-limited; "
                    "backoff #%d for %.0fs (unlocked %d transient)",
                    backoff_idx, wait, unlocked,
                )
                await asyncio.sleep(wait)

            try:
                return await self._complete_one_round(request)
            except RateLimitError as exc:
                last_error = exc
                continue
            except RoutingError as exc:
                # No healthy account available. Treat as the same
                # "all rate-limited" case for backoff purposes.
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise RoutingError(
            "Pool exhausted after exponential backoff; no accounts "
            "available.",
        )

    async def _complete_one_round(
        self, request: CompletionRequest,
    ) -> CompletionResponse:
        """One swap-through of all configured accounts. Raises the
        last error (typically RateLimitError) when every account
        is exhausted."""
        last_error: Exception | None = None
        tried: set = set()

        for _ in range(self.max_swap_attempts):
            try:
                account = self.router.pick(
                    request.tier,
                    request.session_key,
                    pinned_account_label=request.pinned_account_label,
                    exclude_ids=frozenset(tried),
                )
            except RoutingError as routing_err:
                if last_error is not None:
                    raise last_error from routing_err
                raise

            provider = self.registry.get_provider(account.provider_name)

            # Same-account retries before falling through. Default is 1
            # extra try (so 2 total attempts on the same account) before
            # we mark it rate-limited and swap to the next priority
            # group. Lets Mike absorb a transient 503 on OpenCode without
            # immediately burning OpenRouter credits.
            same_account_attempts = max(1, 1 + account.retries_before_swap)
            response: CompletionResponse | None = None
            transient_error: Exception | None = None
            rate_limited = False
            # Per-model output-discipline overlay: appends a small
            # nudge to the system message for known closed models
            # whose default output habits don't match our role-prompt
            # scaffolding (no-op for open-weights). See
            # ``korpha/cofounder/prompt_overlays.py``.
            from korpha.cofounder.prompt_overlays import apply_overlay
            model_id = account.tier_models.get(request.tier, "")
            dispatched = apply_overlay(request, model_id)
            for attempt_idx in range(same_account_attempts):
                try:
                    response = await provider.complete(dispatched, account)
                except RateLimitError as exc:
                    transient_error = exc
                    rate_limited = True
                    break
                except ProviderError as exc:
                    transient_error = exc
                    if attempt_idx + 1 >= same_account_attempts:
                        break
                    continue
                else:
                    break

            if response is not None:
                self.router.release(account.id)
                account.spent_this_period_usd += response.cost_usd
                return response

            # All same-account attempts failed for this account.
            self.router.release(account.id)
            if rate_limited and isinstance(transient_error, RateLimitError):
                self.router.mark_rate_limited(
                    account_id=account.id,
                    retry_after_seconds=transient_error.retry_after_seconds,
                    session_key=request.session_key,
                )
            # Force cascade-forward: don't re-pick this account on the
            # next swap iteration even if it's still "healthy" from the
            # router's perspective.
            tried.add(account.id)
            last_error = transient_error
            continue

        if last_error is not None:
            raise last_error
        raise RoutingError("Exhausted swap attempts without a response or specific error.")

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream chunks for the request. Same routing rules as complete():
        session affinity preserved, rate-limit triggers a swap.

        Unlike complete(), we don't transparently retry mid-stream — the
        caller's in-flight UI state would get scrambled. We retry on
        connection-time errors only (RateLimit / ProviderError raised before
        the first chunk yields). Once content has started streaming, errors
        propagate to the caller.
        """
        last_error: Exception | None = None
        tried: set = set()

        for _ in range(self.max_swap_attempts):
            try:
                account = self.router.pick(
                    request.tier,
                    request.session_key,
                    pinned_account_label=request.pinned_account_label,
                    exclude_ids=frozenset(tried),
                )
            except RoutingError as routing_err:
                if last_error is not None:
                    raise last_error from routing_err
                raise

            provider = self.registry.get_provider(account.provider_name)
            # Per-model overlay (see complete() above for rationale).
            from korpha.cofounder.prompt_overlays import apply_overlay
            model_id = account.tier_models.get(request.tier, "")
            dispatched_stream = apply_overlay(request, model_id)
            iterator = provider.stream_complete(dispatched_stream, account)
            try:
                first_chunk = await iterator.__anext__()
            except RateLimitError as exc:
                self.router.release(account.id)
                self.router.mark_rate_limited(
                    account_id=account.id,
                    retry_after_seconds=exc.retry_after_seconds,
                    session_key=request.session_key,
                )
                tried.add(account.id)
                last_error = exc
                continue
            except ProviderError as exc:
                self.router.release(account.id)
                tried.add(account.id)
                last_error = exc
                continue
            except StopAsyncIteration:
                self.router.release(account.id)
                return

            try:
                yield first_chunk
                async for chunk in iterator:
                    yield chunk
            finally:
                self.router.release(account.id)
            return

        if last_error is not None:
            raise last_error
        raise RoutingError("Exhausted swap attempts without a stream.")
