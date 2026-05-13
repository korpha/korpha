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

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from korpha.inference.provider import Provider, ProviderError, RateLimitError
from korpha.inference.registry import ProviderAccount, ProviderRegistry
from korpha.inference.router import InferenceRouter, RoutingError
from korpha.inference.types import CompletionRequest, CompletionResponse, StreamChunk


@dataclass
class InferencePool:
    providers: list[Provider]
    accounts: list[ProviderAccount]
    max_swap_attempts: int = 3
    """How many account swaps to try after rate-limit/error before giving up."""

    registry: ProviderRegistry = field(init=False)
    router: InferenceRouter = field(init=False)

    def __post_init__(self) -> None:
        self.registry = ProviderRegistry()
        for p in self.providers:
            self.registry.register_provider(p)
        for a in self.accounts:
            self.registry.add_account(a)
        self.router = InferenceRouter(registry=self.registry)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
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
            for attempt_idx in range(same_account_attempts):
                try:
                    response = await provider.complete(request, account)
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
            iterator = provider.stream_complete(request, account)
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
