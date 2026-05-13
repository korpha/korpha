"""ProviderRegistry: stores configured Provider implementations and ProviderAccounts.

A ProviderAccount is one (provider, credentials, tier-capabilities) combo.
A user may have multiple accounts on the same provider (e.g. 3x DeepSeek API
keys for parallelism, or 2x Claude Pro logins on local OSS).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from korpha.audit.model import InferenceTier
from korpha.db._base import utcnow

if TYPE_CHECKING:
    from korpha.inference.provider import Provider


class AuthType(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION_CLI = "subscription_cli"
    OAUTH = "oauth"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    RATE_LIMITED = "rate_limited"
    EXHAUSTED = "exhausted"
    DISABLED = "disabled"


@dataclass(frozen=True)
class TierPricing:
    """Per-million-token prices for a tier on this account."""

    input_per_1m_usd: Decimal
    output_per_1m_usd: Decimal
    cached_input_per_1m_usd: Decimal | None = None
    """If None, falls back to a fraction of input_per_1m_usd (default 0.1x)."""


@dataclass
class ProviderAccount:
    """One configured provider+credentials with its tier capabilities."""

    provider_name: str
    auth_type: AuthType
    tier_models: dict[InferenceTier, str]
    """Which model this account uses for each tier it serves."""

    pricing: dict[InferenceTier, TierPricing] = field(default_factory=dict)
    api_key: str | None = None
    concurrency_limit: int = 4
    spend_cap_usd: Decimal | None = None
    spent_this_period_usd: Decimal = Decimal("0")

    priority: int = 100
    """Lower = tried first. Accounts at the same priority within a tier
    are load-balanced as equals. Lets Mike pin OpenCode at 1, Ollama at
    2, OpenRouter at 3 so subscription/local quota burns before paid
    bulk."""

    retries_before_swap: int = 1
    """How many times Pool retries this same account on a transient
    error (rate limit / 5xx) before falling through to the next
    priority. Default 1 = one retry on the same account, then swap."""

    free_tier_quota: dict | None = field(default=None)
    """Free-tier quota descriptor for accounts where 429 means
    'daily/hourly cap consumed', not 'slow down'. Shape:
    ``{"window_kind": "daily"|"hourly"|"monthly", "reset_utc": "00:00"}``.
    None means treat 429 as standard rate-limit-with-retry-after."""

    id: UUID = field(default_factory=uuid4)
    status: AccountStatus = AccountStatus.ACTIVE
    rate_limit_until: datetime | None = None
    last_used_at: datetime | None = None

    label: str | None = None

    def serves_tier(self, tier: InferenceTier) -> bool:
        return tier in self.tier_models

    def is_healthy(self, *, now: datetime | None = None) -> bool:
        if self.status in (AccountStatus.DISABLED, AccountStatus.EXHAUSTED):
            return False
        now = now or utcnow()
        if self.rate_limit_until and now < self.rate_limit_until:
            return False
        return not (
            self.spend_cap_usd is not None
            and self.spent_this_period_usd >= self.spend_cap_usd
        )


class ProviderRegistry:
    """Holds Provider implementations + configured ProviderAccounts."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._accounts: list[ProviderAccount] = []

    def register_provider(self, provider: Provider) -> None:
        self._providers[provider.name] = provider

    def get_provider(self, name: str) -> Provider:
        if name not in self._providers:
            raise KeyError(f"Provider {name!r} not registered")
        return self._providers[name]

    def add_account(self, account: ProviderAccount) -> None:
        if account.provider_name not in self._providers:
            raise ValueError(
                f"Account references unregistered provider {account.provider_name!r}"
            )
        self._accounts.append(account)

    def accounts(self) -> list[ProviderAccount]:
        return list(self._accounts)

    def healthy_accounts_for_tier(
        self,
        tier: InferenceTier,
        *,
        now: datetime | None = None,
    ) -> list[ProviderAccount]:
        return [a for a in self._accounts if a.serves_tier(tier) and a.is_healthy(now=now)]
