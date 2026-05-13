"""Tests for cascade ordering + retries-before-swap + free-tier 429."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.pool import InferencePool
from korpha.inference.provider import (
    Provider, ProviderError, RateLimitError,
)
from korpha.inference.registry import (
    AccountStatus, AuthType, ProviderAccount,
)
from korpha.inference.router import (
    InferenceRouter, RoutingError, next_quota_reset,
)
from korpha.inference.types import (
    CompletionRequest, CompletionResponse, Message, Role,
)


def _make_account(
    name: str, *, priority: int = 100,
    retries_before_swap: int = 1,
    free_tier_quota: dict | None = None,
    label: str | None = None,
    concurrency: int = 4,
) -> ProviderAccount:
    return ProviderAccount(
        provider_name=name,
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.WORKHORSE: f"{name}-model"},
        api_key="k",
        concurrency_limit=concurrency,
        priority=priority,
        retries_before_swap=retries_before_swap,
        free_tier_quota=free_tier_quota,
        label=label or name,
    )


def _stub_provider(name: str) -> Provider:
    p = MagicMock(spec=Provider)
    p.name = name
    return p


def _make_pool(accounts: list[ProviderAccount]) -> InferencePool:
    providers = []
    seen = set()
    for a in accounts:
        if a.provider_name not in seen:
            providers.append(_stub_provider(a.provider_name))
            seen.add(a.provider_name)
    return InferencePool(providers=providers, accounts=accounts)


def _req() -> CompletionRequest:
    return CompletionRequest(
        tier=InferenceTier.WORKHORSE,
        messages=[Message(role=Role.USER, content="hi")],
        session_key="s1",
    )


def _resp(cost: float = 0.001) -> CompletionResponse:
    return CompletionResponse(
        content="ok", tool_calls=(),
        input_tokens=1, output_tokens=1, cached_tokens=0,
        cost_usd=Decimal(str(cost)),
        provider="x", model="x-model", account_id="",
        finish_reason="stop",
    )


# ---------------- priority / cascade ----------------

def test_router_picks_lowest_priority_first() -> None:
    a1 = _make_account("primary", priority=1)
    a2 = _make_account("backup", priority=2)
    a3 = _make_account("paid_bulk", priority=3)
    pool = _make_pool([a3, a2, a1])  # registration order shouldn't matter
    picked = pool.router.pick(InferenceTier.WORKHORSE, "s1")
    assert picked.label == "primary"


def test_router_load_balances_within_priority_tie() -> None:
    a1 = _make_account("free-key-1", priority=4, label="k1")
    a2 = _make_account("free-key-2", priority=4, label="k2")
    a3 = _make_account("free-key-3", priority=4, label="k3")
    pool = _make_pool([a1, a2, a3])
    # Without affinity (distinct session keys), spread should hit each
    picks = [
        pool.router.pick(InferenceTier.WORKHORSE, f"session-{i}").label
        for i in range(3)
    ]
    # All three sessions get distinct accounts (round-robin via least-loaded).
    # Note in_flight stays high since we never release — fine for the test.
    assert set(picks) == {"k1", "k2", "k3"}


def test_router_falls_through_to_next_priority_when_primary_rate_limited() -> None:
    a1 = _make_account("primary", priority=1)
    a2 = _make_account("backup", priority=2)
    pool = _make_pool([a1, a2])
    pool.router.mark_rate_limited(a1.id, retry_after_seconds=300.0)
    picked = pool.router.pick(InferenceTier.WORKHORSE, "s1")
    assert picked.label == "backup"


def test_router_session_affinity_only_within_top_priority() -> None:
    """When primary recovers, sessions previously pinned to fallback
    should migrate back rather than stay stuck on the more expensive
    provider."""
    a1 = _make_account("primary", priority=1)
    a2 = _make_account("backup", priority=2)
    pool = _make_pool([a1, a2])

    # Primary unhealthy → session pins to backup
    pool.router.mark_rate_limited(a1.id, retry_after_seconds=0.001)
    p1 = pool.router.pick(InferenceTier.WORKHORSE, "s1")
    assert p1.label == "backup"

    # Primary recovers — clear rate-limit
    pool.router.reset_account_status(a1.id)

    # Same session should now route to primary, not stay on backup
    p2 = pool.router.pick(InferenceTier.WORKHORSE, "s1")
    assert p2.label == "primary"


# ---------------- retries_before_swap ----------------

@pytest.mark.asyncio
async def test_pool_retries_same_account_before_swap() -> None:
    """retries_before_swap=2 means we hit the same account 3 times
    (1 + 2 retries) on transient ProviderError before swapping."""
    a1 = _make_account("primary", priority=1, retries_before_swap=2)
    a2 = _make_account("backup", priority=2)
    pool = _make_pool([a1, a2])

    primary = pool.registry.get_provider("primary")
    backup = pool.registry.get_provider("backup")

    # First two attempts on primary fail, third succeeds
    primary.complete = AsyncMock(side_effect=[
        ProviderError("transient"),
        ProviderError("transient"),
        _resp(),
    ])
    backup.complete = AsyncMock(return_value=_resp())

    response = await pool.complete(_req())
    assert response.content == "ok"
    assert primary.complete.await_count == 3
    assert backup.complete.await_count == 0


@pytest.mark.asyncio
async def test_pool_swaps_after_exhausting_same_account_retries() -> None:
    a1 = _make_account("primary", priority=1, retries_before_swap=1)
    a2 = _make_account("backup", priority=2)
    pool = _make_pool([a1, a2])

    primary = pool.registry.get_provider("primary")
    backup = pool.registry.get_provider("backup")

    # Primary fails both attempts (initial + 1 retry); backup succeeds
    primary.complete = AsyncMock(side_effect=[
        ProviderError("transient"),
        ProviderError("transient"),
    ])
    backup.complete = AsyncMock(return_value=_resp())

    response = await pool.complete(_req())
    assert response.content == "ok"
    assert primary.complete.await_count == 2
    assert backup.complete.await_count == 1


@pytest.mark.asyncio
async def test_pool_rate_limit_skips_retries_and_swaps() -> None:
    """A 429 is a hard signal that this account is unavailable —
    don't waste another attempt on it, swap immediately."""
    a1 = _make_account("primary", priority=1, retries_before_swap=3)
    a2 = _make_account("backup", priority=2)
    pool = _make_pool([a1, a2])

    primary = pool.registry.get_provider("primary")
    backup = pool.registry.get_provider("backup")

    primary.complete = AsyncMock(side_effect=RateLimitError(
        "rate", retry_after_seconds=60.0,
    ))
    backup.complete = AsyncMock(return_value=_resp())

    response = await pool.complete(_req())
    assert response.content == "ok"
    # Only one primary attempt despite retries_before_swap=3
    assert primary.complete.await_count == 1
    assert backup.complete.await_count == 1


# ---------------- free-tier 429 semantics ----------------

def test_next_quota_reset_daily_today_already_passed() -> None:
    now = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)  # 14:00 UTC
    reset = next_quota_reset({"window_kind": "daily", "reset_utc": "00:00"}, now=now)
    assert reset == datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)


def test_next_quota_reset_daily_today_still_ahead() -> None:
    now = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    reset = next_quota_reset({"window_kind": "daily", "reset_utc": "18:00"}, now=now)
    assert reset == datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc)


def test_next_quota_reset_hourly() -> None:
    now = datetime(2026, 5, 12, 14, 23, tzinfo=timezone.utc)
    reset = next_quota_reset({"window_kind": "hourly"}, now=now)
    assert reset == datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc)


def test_next_quota_reset_monthly_rolls_year() -> None:
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)
    reset = next_quota_reset({"window_kind": "monthly"}, now=now)
    assert reset == datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_router_free_tier_429_uses_quota_reset_not_retry_after() -> None:
    """Free-tier account 429 should wait for the daily reset, not
    OpenRouter's bogus 1-second retry_after."""
    free = _make_account(
        "openrouter", priority=4,
        free_tier_quota={"window_kind": "daily", "reset_utc": "00:00"},
        label="free-1",
    )
    pool = _make_pool([free])
    before = datetime.now(timezone.utc)

    # Server says "retry in 1 second"; we should ignore that.
    pool.router.mark_rate_limited(free.id, retry_after_seconds=1.0)

    assert free.rate_limit_until is not None
    # rate_limit_until is at least the next midnight (way more than 1 second from now)
    delta = free.rate_limit_until - before
    assert delta > timedelta(minutes=5), (
        f"Free-tier 429 should wait for reset, got delta={delta}"
    )


def test_router_paid_429_still_uses_retry_after() -> None:
    """Non-free-tier accounts use the standard retry_after."""
    paid = _make_account("openrouter", priority=3, label="paid")
    pool = _make_pool([paid])
    before = datetime.now(timezone.utc)
    pool.router.mark_rate_limited(paid.id, retry_after_seconds=10.0)
    assert paid.rate_limit_until is not None
    delta = paid.rate_limit_until - before
    assert timedelta(seconds=9) < delta < timedelta(seconds=12)


# ---------------- vision tier auto-registration ----------------

def test_env_fallback_registers_nvidia_vision(monkeypatch) -> None:
    """Setting NVIDIA_API_KEY should auto-register the VISION tier
    with Nemotron, not just WORKHORSE + PRO."""
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    # Make sure no other presets clutter the result
    for var in ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "OPENCODE_API_KEY", "DEEPSEEK_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    from korpha.inference.env_fallback import detect_configured_providers
    pairs = detect_configured_providers()
    nvidia_accounts = [a for _, a in pairs if a.label == "nvidia-nim"]
    assert len(nvidia_accounts) == 1
    assert InferenceTier.VISION in nvidia_accounts[0].tier_models
    assert "nemotron" in nvidia_accounts[0].tier_models[InferenceTier.VISION].lower()


def test_env_fallback_registers_openrouter_vision(monkeypatch) -> None:
    """OpenRouter as the vision fallback — free Nemotron variant."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    for var in ["NVIDIA_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "OPENCODE_API_KEY", "DEEPSEEK_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    from korpha.inference.env_fallback import detect_configured_providers
    pairs = detect_configured_providers()
    or_accounts = [a for _, a in pairs if a.label == "openrouter"]
    assert len(or_accounts) == 1
    assert InferenceTier.VISION in or_accounts[0].tier_models
    assert ":free" in or_accounts[0].tier_models[InferenceTier.VISION]


def test_env_fallback_skips_vision_tier_for_text_only_provider(monkeypatch) -> None:
    """A preset without a vision_model should NOT register VISION —
    routing there would silently fail at request time."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    for var in ["NVIDIA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "OPENCODE_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    from korpha.inference.env_fallback import detect_configured_providers
    pairs = detect_configured_providers()
    ds_accounts = [a for _, a in pairs if a.label == "deepseek"]
    assert len(ds_accounts) == 1
    assert InferenceTier.VISION not in ds_accounts[0].tier_models


# ---------------- no-healthy-accounts edge case ----------------

def test_router_raises_when_all_accounts_rate_limited() -> None:
    a = _make_account("only", priority=1)
    pool = _make_pool([a])
    pool.router.mark_rate_limited(a.id, retry_after_seconds=3600.0)
    with pytest.raises(RoutingError):
        pool.router.pick(InferenceTier.WORKHORSE, "s1")
