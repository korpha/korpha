"""Credits — a refillable per-business wallet of agent-action allowance.

Where :class:`ActionThrottle` is a rate limit (rolling window, never
refills early), a :class:`CreditPool` is a wallet: a granted monthly
balance that can be topped up, debited per action, and refilled on
a cadence. The shape:

  - Each business has at most one CreditPool.
  - ``balance`` is the current spendable credit count.
  - ``monthly_grant`` is the recurring allowance — refills on
    ``next_refill_at`` rollover.
  - ``lifetime_granted`` / ``lifetime_purchased`` track totals for
    accounting + product analytics.
  - :class:`CreditLedger` rows are an append-only audit of every
    grant / topup / debit / refund.

The autonomy daemon's :func:`evaluate` treats a CreditPool with
balance ≤ 0 as a pause reason (``credits_exhausted``). Manual UI /
CLI actions are NOT blocked by credits in main code — that's the
operator's choice to wire in. A hosted-deployment wrapper can
subscribe to plugin hooks to deduct + block at any granularity it
wants.

Topup integration is intentionally minimal here: the
:meth:`CreditService.topup` method takes an amount + reference string
and writes a ledger entry. Wiring it to Stripe / PayPal / etc. is
the integrator's job — that's not main-code territory.
"""
from korpha.credits.model import (
    CreditLedger,
    CreditLedgerKind,
    CreditPool,
)
from korpha.credits.service import (
    CreditService,
    InsufficientCreditsError,
)

__all__ = [
    "CreditLedger",
    "CreditLedgerKind",
    "CreditPool",
    "CreditService",
    "InsufficientCreditsError",
]
