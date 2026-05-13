"""Per-account liveness probe.

Sends a 1-token request to each configured account and reports whether
the response succeeded, was rate-limited, or otherwise failed. Used by
``korpha inference probe`` and surfaced on /app/providers so Mike
can answer "which free OpenRouter key is alive right now" without
guessing.

We deliberately keep the probe cheap (max_tokens=1, no system prompt)
so running it against 13 free OpenRouter keys doesn't itself burn the
daily quota.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from korpha.audit.model import InferenceTier
from korpha.inference.provider import (
    Provider, ProviderError, RateLimitError,
)
from korpha.inference.registry import ProviderAccount
from korpha.inference.types import CompletionRequest


@dataclass
class ProbeResult:
    label: str
    provider: str
    ok: bool
    """True when a real completion came back (or empty completion w/o error)."""

    message: str = ""
    """Short human-readable status. For 429 we surface 'rate-limited'
    plus the retry-after seconds. For other failures we surface the
    error class + first 80 chars of the body."""

    reset_utc: str | None = None
    """When the rate-limit window resets, if the server told us
    (X-RateLimit-Reset header on OpenRouter) or we have it
    configured on the account."""


def _provider_by_name(providers: list[Provider], name: str) -> Provider | None:
    for p in providers:
        if p.name == name:
            return p
    return None


async def _probe_one(
    provider: Provider, account: ProviderAccount,
) -> ProbeResult:
    label = account.label or account.provider_name
    # Pick the cheapest tier the account serves. Free-tier accounts
    # often only serve workhorse; we don't want to spend pro-tier
    # tokens just to probe.
    tier = (
        InferenceTier.WORKHORSE
        if account.serves_tier(InferenceTier.WORKHORSE)
        else next(iter(account.tier_models))
    )
    req = CompletionRequest(
        tier=tier,
        messages=[{"role": "user", "content": "ok"}],
        max_tokens=1,
        session_key=f"probe:{account.id}",
    )
    try:
        await provider.complete(req, account)
    except RateLimitError as exc:
        msg = f"rate-limited (retry_after={exc.retry_after_seconds:.0f}s)"
        reset = (
            account.free_tier_quota.get("reset_utc")
            if isinstance(account.free_tier_quota, dict) else None
        )
        return ProbeResult(
            label=label, provider=provider.name, ok=False,
            message=msg, reset_utc=reset,
        )
    except ProviderError as exc:
        return ProbeResult(
            label=label, provider=provider.name, ok=False,
            message=f"{type(exc).__name__}: {str(exc)[:80]}",
        )
    except Exception as exc:  # noqa: BLE001 - we want to surface any failure
        return ProbeResult(
            label=label, provider=provider.name, ok=False,
            message=f"{type(exc).__name__}: {str(exc)[:80]}",
        )
    return ProbeResult(label=label, provider=provider.name, ok=True)


async def probe_accounts(
    providers: list[Provider], accounts: list[ProviderAccount],
) -> list[ProbeResult]:
    """Probe each account in parallel. Returns one ProbeResult per
    account in the input order."""
    tasks = []
    for a in accounts:
        provider = _provider_by_name(providers, a.provider_name)
        if provider is None:
            tasks.append(asyncio.sleep(0, result=ProbeResult(
                label=a.label or a.provider_name,
                provider=a.provider_name,
                ok=False,
                message="provider not registered",
            )))
        else:
            tasks.append(_probe_one(provider, a))
    return list(await asyncio.gather(*tasks))


__all__ = ["ProbeResult", "probe_accounts"]
