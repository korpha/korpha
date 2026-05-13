"""Inference Pool tests — routing, session affinity, rate-limit swap, spend caps."""
from __future__ import annotations

from decimal import Decimal

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference import (
    CompletionRequest,
    InferencePool,
    Message,
    MockProvider,
    ProviderAccount,
    Role,
    RoutingError,
    TierPricing,
)
from korpha.inference.registry import AccountStatus, AuthType


def _account(label: str, *, concurrency: int = 4, spend_cap: Decimal | None = None) -> ProviderAccount:
    return ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "mock-flash",
            InferenceTier.PRO: "mock-pro",
            InferenceTier.CONSULTANT: "mock-consultant",
        },
        pricing={
            InferenceTier.WORKHORSE: TierPricing(
                input_per_1m_usd=Decimal("0.10"),
                output_per_1m_usd=Decimal("0.20"),
            ),
            InferenceTier.PRO: TierPricing(
                input_per_1m_usd=Decimal("0.50"),
                output_per_1m_usd=Decimal("1.00"),
                cached_input_per_1m_usd=Decimal("0.05"),
            ),
            InferenceTier.CONSULTANT: TierPricing(
                input_per_1m_usd=Decimal("3.00"),
                output_per_1m_usd=Decimal("15.00"),
            ),
        },
        api_key="sk-test",
        concurrency_limit=concurrency,
        spend_cap_usd=spend_cap,
        label=label,
    )


def _request(session_key: str, *, tier: InferenceTier = InferenceTier.PRO) -> CompletionRequest:
    return CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content="You are a helpful CEO."),
            Message(role=Role.USER, content=f"Plan the week for session {session_key}."),
        ],
        tier=tier,
        session_key=session_key,
    )


@pytest.mark.asyncio
async def test_basic_completion() -> None:
    provider = MockProvider()
    accounts = [_account("primary")]
    pool = InferencePool(providers=[provider], accounts=accounts)

    response = await pool.complete(_request("session-1"))

    assert response.provider == "mock"
    assert response.account_id == str(accounts[0].id)
    assert response.input_tokens > 0
    assert response.output_tokens > 0
    assert response.cost_usd > 0


@pytest.mark.asyncio
async def test_session_affinity_pins_same_account() -> None:
    """Same session_key → same account across calls (cache-hit preservation)."""
    provider = MockProvider()
    a, b, c = _account("a"), _account("b"), _account("c")
    pool = InferencePool(providers=[provider], accounts=[a, b, c])

    r1 = await pool.complete(_request("session-x"))
    r2 = await pool.complete(_request("session-x"))
    r3 = await pool.complete(_request("session-x"))

    assert r1.account_id == r2.account_id == r3.account_id


@pytest.mark.asyncio
async def test_cross_session_distributes_load() -> None:
    """Different session_keys spread across accounts."""
    provider = MockProvider()
    a, b, c = _account("a"), _account("b"), _account("c")
    pool = InferencePool(providers=[provider], accounts=[a, b, c])

    accounts_used: set[str] = set()
    for i in range(6):
        r = await pool.complete(_request(f"session-{i}"))
        accounts_used.add(r.account_id)

    assert len(accounts_used) >= 2, "expected load to spread across multiple accounts"


@pytest.mark.asyncio
async def test_rate_limit_swaps_to_other_account() -> None:
    """When the chosen account hits a rate limit, the next pick uses a different one."""
    a, b = _account("a"), _account("b")
    provider = MockProvider(rate_limit_account_ids={str(a.id)})
    pool = InferencePool(providers=[provider], accounts=[a, b])

    # First call: a is rate-limited → swap to b → succeed.
    response = await pool.complete(_request("session-y"))
    assert response.account_id == str(b.id)

    # a should be marked unhealthy.
    assert a.status == AccountStatus.RATE_LIMITED


@pytest.mark.asyncio
async def test_routing_error_when_no_healthy_account() -> None:
    a = _account("a")
    a.status = AccountStatus.DISABLED
    provider = MockProvider()
    pool = InferencePool(providers=[provider], accounts=[a])

    with pytest.raises(RoutingError):
        await pool.complete(_request("session-z"))


@pytest.mark.asyncio
async def test_spend_cap_marks_account_unhealthy() -> None:
    """Account at spend cap is excluded from routing."""
    a = _account("a", spend_cap=Decimal("0.000001"))  # microscopic cap
    b = _account("b")
    provider = MockProvider()
    pool = InferencePool(providers=[provider], accounts=[a, b])

    # First call may go to either; after one call to a, its spent likely > cap.
    r1 = await pool.complete(_request("session-1"))
    assert r1.account_id in {str(a.id), str(b.id)}

    # Force a to be over cap to exercise the unhealthy path deterministically.
    a.spent_this_period_usd = Decimal("9.99")

    # Subsequent calls should route to b only.
    for i in range(3):
        r = await pool.complete(_request(f"session-{i+2}"))
        assert r.account_id == str(b.id)


@pytest.mark.asyncio
async def test_cost_reflects_cache_ratio() -> None:
    """A higher cache_hit_ratio reduces cost."""
    provider_no_cache = MockProvider(cache_hit_ratio=0.0)
    provider_high_cache = MockProvider(cache_hit_ratio=0.9)

    pool_a = InferencePool(providers=[provider_no_cache], accounts=[_account("a")])
    pool_b = InferencePool(providers=[provider_high_cache], accounts=[_account("b")])

    r_no_cache = await pool_a.complete(_request("session-no-cache"))
    r_with_cache = await pool_b.complete(_request("session-cached"))

    assert r_with_cache.cost_usd < r_no_cache.cost_usd
    assert r_with_cache.cache_hit_ratio == pytest.approx(0.9, abs=0.05)


@pytest.mark.asyncio
async def test_session_affinity_breaks_after_rate_limit() -> None:
    """Session pinned to A → A rate-limited → next call for same session goes to B."""
    a, b = _account("a"), _account("b")
    provider = MockProvider()  # initially no rate limits
    pool = InferencePool(providers=[provider], accounts=[a, b])

    r1 = await pool.complete(_request("sticky-session"))
    pinned_account = r1.account_id

    # Force the pinned account to rate-limit on next call.
    provider.rate_limit_account_ids = {pinned_account}

    r2 = await pool.complete(_request("sticky-session"))
    assert r2.account_id != pinned_account


def test_account_not_serving_tier_excluded() -> None:
    """An account that has no model for a tier is not selected for that tier."""
    a = ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.WORKHORSE: "mock-flash"},  # only workhorse
        api_key="x",
    )
    provider = MockProvider()
    pool = InferencePool(providers=[provider], accounts=[a])

    with pytest.raises(RoutingError):
        pool.router.pick(InferenceTier.PRO, "any-session")
