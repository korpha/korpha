"""Mock provider for tests and local development.

Deterministic responses, configurable cache-hit simulation, optional
forced rate limits. No network calls — runs offline.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from korpha.inference.provider import Provider, RateLimitError
from korpha.inference.registry import ProviderAccount, TierPricing
from korpha.inference.types import CompletionRequest, CompletionResponse


@dataclass
class _CallRecord:
    request: CompletionRequest
    account_id: str


@dataclass
class MockProvider(Provider):
    """Deterministic offline provider used for tests and dry runs."""

    name: str = "mock"
    rate_limit_account_ids: set[str] = field(default_factory=set)
    cache_hit_ratio: float = 0.0
    """Fraction of input_tokens to count as cached. 1.0 = full prefix hit."""

    response_template: str = "[mock] {summary}"
    static_response: str | None = None
    """When set, return this verbatim instead of formatting the template.
    Useful for returning JSON content that contains literal `{` characters."""

    latency_seconds: float = 0.0
    calls: list[_CallRecord] = field(default_factory=list)

    async def complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> CompletionResponse:
        if str(account.id) in self.rate_limit_account_ids:
            raise RateLimitError(account_id=str(account.id), retry_after_seconds=1.0)

        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

        prompt_text = "\n".join(m.content for m in request.messages)
        digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]
        if self.static_response is not None:
            content = self.static_response
        else:
            content = self.response_template.format(
                summary=f"len={len(prompt_text)} sha={digest}"
            )

        input_tokens = max(1, len(prompt_text) // 4)
        output_tokens = max(1, len(content) // 4)
        cached_tokens = int(input_tokens * self.cache_hit_ratio)

        cost = self._estimate_cost(account, request, input_tokens, output_tokens, cached_tokens)

        self.calls.append(_CallRecord(request=request, account_id=str(account.id)))

        model = account.tier_models.get(request.tier, "mock-default")
        return CompletionResponse(
            content=content,
            tool_calls=(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=cost,
            provider=self.name,
            model=model,
            account_id=str(account.id),
            cache_hit_ratio=cached_tokens / input_tokens if input_tokens else 0.0,
        )

    @staticmethod
    def _estimate_cost(
        account: ProviderAccount,
        request: CompletionRequest,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> Decimal:
        pricing = account.pricing.get(request.tier)
        if pricing is None:
            return Decimal("0")
        return _compute_cost(pricing, input_tokens, output_tokens, cached_tokens)


def _compute_cost(
    pricing: TierPricing,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
) -> Decimal:
    fresh_input = max(input_tokens - cached_tokens, 0)
    cached_price = (
        pricing.cached_input_per_1m_usd
        if pricing.cached_input_per_1m_usd is not None
        else pricing.input_per_1m_usd / Decimal("10")
    )
    cost = (
        Decimal(fresh_input) * pricing.input_per_1m_usd / Decimal("1_000_000")
        + Decimal(cached_tokens) * cached_price / Decimal("1_000_000")
        + Decimal(output_tokens) * pricing.output_per_1m_usd / Decimal("1_000_000")
    )
    return cost.quantize(Decimal("0.000001"))
