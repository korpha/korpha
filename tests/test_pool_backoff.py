"""Tests for InferencePool exponential backoff on whole-pool 429.

Covers the new behavior added when all 13 OpenRouter free keys hit
429 simultaneously: instead of immediately raising RateLimitError,
the pool sleeps + retries with exponential backoff to catch the
transient case where the throttle clears in seconds.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.pool import InferencePool
from korpha.inference.provider import Provider, RateLimitError
from korpha.inference.registry import AccountStatus, AuthType, ProviderAccount
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    Role,
)


class _AlwaysRateLimitProvider(Provider):
    """Test provider that always 429s + counts calls."""

    name = "always-429"

    def __init__(self, retry_after_seconds: float = 5.0):
        self.calls = 0
        self.retry_after_seconds = retry_after_seconds

    async def complete(self, request, account):
        self.calls += 1
        raise RateLimitError(
            account_id=str(account.id),
            retry_after_seconds=self.retry_after_seconds,
        )

    async def stream_complete(self, request, account):
        if False:
            yield
        raise RateLimitError(
            account_id=str(account.id),
            retry_after_seconds=self.retry_after_seconds,
        )


class _CountdownTo200Provider(Provider):
    """Test provider that 429s for the first N calls then returns
    a real response. Used to simulate "transient throttle that
    clears after a few retries"."""

    name = "always-429"  # share name with rate-limit provider

    def __init__(self, fail_count: int = 2):
        self.calls = 0
        self.fail_count = fail_count

    async def complete(self, request, account):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RateLimitError(
                account_id=str(account.id),
                retry_after_seconds=5.0,
            )
        return CompletionResponse(
            content="ok",
            tool_calls=(),
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            cost_usd=Decimal("0"),
            provider="always-429",
            model="test-model",
            account_id=str(account.id),
        )

    async def stream_complete(self, request, account):  # pragma: no cover
        if False:
            yield


def _make_accounts(
    count: int, *, free_tier: bool = False,
) -> list[ProviderAccount]:
    """N accounts that share the same test provider.

    ``free_tier=True`` adds free_tier_quota so the router locks
    them until tomorrow's reset (used to test the daily-quota path).
    Default False = router honors retry_after, mimicking OpenRouter's
    transient throttle case."""
    kwargs = {
        "provider_name": "always-429",
        "auth_type": AuthType.API_KEY,
        "tier_models": {InferenceTier.PRO: "test-model"},
    }
    accounts = []
    for i in range(count):
        acc = ProviderAccount(
            **kwargs,
            api_key=f"key-{i}",
            label=f"acct-{i}",
        )
        if free_tier:
            acc.free_tier_quota = {
                "window_kind": "daily", "reset_utc": "00:00",
            }
        accounts.append(acc)
    return accounts


def _make_request() -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role=Role.USER, content="hi")],
        tier=InferenceTier.PRO,
        session_key=f"test-{uuid4().hex[:8]}",
    )


# ---- max_swap_attempts auto-sizes to account count ---------------


@pytest.mark.anyio
async def test_max_swap_attempts_defaults_to_account_count():
    provider = _AlwaysRateLimitProvider()
    accounts = _make_accounts(13)
    pool = InferencePool(
        providers=[provider],
        accounts=accounts,
        pool_backoff_seconds=(),  # disable backoff for this test
    )
    assert pool.max_swap_attempts >= 13


@pytest.mark.anyio
async def test_max_swap_attempts_respects_explicit_value():
    provider = _AlwaysRateLimitProvider()
    accounts = _make_accounts(13)
    pool = InferencePool(
        providers=[provider],
        accounts=accounts,
        max_swap_attempts=5,
        pool_backoff_seconds=(),
    )
    assert pool.max_swap_attempts == 5


# ---- swap tries every account before raising --------------------


@pytest.mark.anyio
async def test_single_round_tries_every_account_before_failing():
    """With 13 accounts and no backoff, the pool should try all 13
    once and then raise the rate-limit error."""
    provider = _AlwaysRateLimitProvider()
    accounts = _make_accounts(13)
    pool = InferencePool(
        providers=[provider],
        accounts=accounts,
        pool_backoff_seconds=(),
    )
    with pytest.raises(RateLimitError):
        await pool.complete(_make_request())
    # 13 accounts × 1 try each (retries_before_swap default = 1
    # extra retry but RateLimitError breaks the inner loop after 1).
    assert provider.calls == 13


# ---- exponential backoff kicks in + recovers --------------------


@pytest.mark.anyio
async def test_backoff_retries_after_pool_exhausted(monkeypatch):
    """When all accounts 429 on the first round, the pool should
    sleep + retry. With short retry_after (transient), the accounts
    get unlocked on backoff and tried again."""
    # Provider that fails on first 13 calls (each account's first
    # attempt), then succeeds. Simulates "OpenRouter transient
    # throttle clears after a couple seconds".
    provider = _CountdownTo200Provider(fail_count=13)
    accounts = _make_accounts(13)

    # Patch asyncio.sleep so the test doesn't actually wait 2s+4s.
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("korpha.inference.pool.asyncio.sleep", fake_sleep)

    pool = InferencePool(
        providers=[provider],
        accounts=accounts,
        pool_backoff_seconds=(2.0, 4.0, 8.0),
    )
    response = await pool.complete(_make_request())
    assert response.content == "ok"
    # First round: 13 calls all fail. Backoff #1 (2s sleep + unlock).
    # Round 2: account 14th call succeeds (counter at 14, > 13).
    assert len(sleeps) >= 1
    assert sleeps[0] == 2.0


@pytest.mark.anyio
async def test_backoff_gives_up_when_no_transient_accounts(monkeypatch):
    """When every rate-limited account is locked until tomorrow (daily
    quota truly exhausted), backoff stops early — no point waiting
    for accounts that won't free up for hours."""
    provider = _AlwaysRateLimitProvider(retry_after_seconds=86400)
    accounts = _make_accounts(3, free_tier=True)  # daily-quota lock

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("korpha.inference.pool.asyncio.sleep", fake_sleep)

    pool = InferencePool(
        providers=[provider],
        accounts=accounts,
        pool_backoff_seconds=(2.0, 4.0, 8.0),
    )
    with pytest.raises(RateLimitError):
        await pool.complete(_make_request())
    # First round 3 calls × 1 attempt = 3. Pool tries backoff #1:
    # sleep 2s, but _unlock_transient_accounts() finds 0 (all locked
    # until tomorrow), aborts. Sleeps may be 1 (we sleep BEFORE
    # checking) or 0 depending on order.
    assert len(sleeps) <= 1


@pytest.mark.anyio
async def test_unlock_transient_flips_short_locks_active():
    """rate_limit_until within the transient horizon flips the
    account back to ACTIVE on retry round."""
    provider = _AlwaysRateLimitProvider()
    accounts = _make_accounts(3)
    pool = InferencePool(
        providers=[provider], accounts=accounts,
    )
    # Manually mark all accounts rate-limited with a short delay.
    now = datetime.now(tz=timezone.utc)
    for a in pool.accounts:
        a.status = AccountStatus.RATE_LIMITED
        a.rate_limit_until = now + timedelta(seconds=10)  # transient
    unlocked = pool._unlock_transient_accounts()
    assert unlocked == 3
    for a in pool.accounts:
        assert a.status == AccountStatus.ACTIVE
        assert a.rate_limit_until is None


@pytest.mark.anyio
async def test_unlock_skips_daily_quota_accounts():
    provider = _AlwaysRateLimitProvider()
    accounts = _make_accounts(3)
    pool = InferencePool(
        providers=[provider], accounts=accounts,
    )
    now = datetime.now(tz=timezone.utc)
    for a in pool.accounts:
        a.status = AccountStatus.RATE_LIMITED
        a.rate_limit_until = now + timedelta(hours=12)  # tomorrow
    unlocked = pool._unlock_transient_accounts()
    assert unlocked == 0
    for a in pool.accounts:
        assert a.status == AccountStatus.RATE_LIMITED


@pytest.fixture
def anyio_backend():
    return "asyncio"
