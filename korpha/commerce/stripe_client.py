"""Tiny Stripe client — just what Korpha needs, no SDK drag.

The full ``stripe`` Python SDK is a 30k-LOC dependency that ships its
own HTTP client, retry policy, and event loop integration. We need
exactly two endpoints today (POST /products, POST /prices, POST
/payment_links), so we hit the REST API directly via httpx and skip
the dep.

If the surface area grows (subscriptions, refunds, webhook signing,
Connect accounts) we can swap to the SDK later behind this client —
the public ``StripeClient`` shape doesn't change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from korpha.inference.limits import request_timeout

_STRIPE_API_BASE = "https://api.stripe.com/v1"


class StripeError(RuntimeError):
    """Stripe rejected the call. Carries the human-readable error message
    Stripe returns so the caller can surface it to the founder."""


@dataclass(frozen=True)
class PaymentLink:
    id: str
    url: str
    """Public URL Mike shares with customers."""

    product_id: str
    price_id: str
    amount_minor: int
    """The amount in the smallest currency unit (cents for USD)."""

    currency: str


@dataclass
class StripeClient:
    """Minimal Stripe v1 REST client."""

    api_key: str
    """``sk_test_...`` for test mode, ``sk_live_...`` for production. Never
    log this; treat like a database password."""

    api_base: str = _STRIPE_API_BASE
    """Override for tests pointing at a mocked transport."""

    timeout_seconds: float = field(default_factory=request_timeout)
    """HTTP timeout in seconds. Defaults to the global
    ``request_timeout()`` floor (60s) so users can override it in
    ``providers.yaml`` ``defaults: request_timeout_seconds:`` instead
    of touching code."""

    _client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def create_payment_link(
        self,
        *,
        name: str,
        amount_usd: float,
        description: str | None = None,
        currency: str = "usd",
    ) -> PaymentLink:
        """Create a one-shot Payment Link for ``amount_usd`` against a new
        product+price. Returns the live URL Mike can share with customers.

        Raises StripeError on any non-2xx response.
        """
        if amount_usd <= 0:
            raise StripeError("amount_usd must be > 0")
        amount_minor = round(amount_usd * 100)

        product = await self._post(
            "products",
            {"name": name, **({"description": description} if description else {})},
        )
        product_id = str(product["id"])

        price = await self._post(
            "prices",
            {
                "product": product_id,
                "unit_amount": amount_minor,
                "currency": currency.lower(),
            },
        )
        price_id = str(price["id"])

        link = await self._post(
            "payment_links",
            {"line_items[0][price]": price_id, "line_items[0][quantity]": 1},
        )
        return PaymentLink(
            id=str(link["id"]),
            url=str(link["url"]),
            product_id=product_id,
            price_id=price_id,
            amount_minor=amount_minor,
            currency=currency.lower(),
        )

    async def _post(
        self, path: str, data: dict[str, str | int]
    ) -> dict[str, object]:
        url = f"{self.api_base.rstrip('/')}/{path}"
        try:
            resp = await self._http().post(
                url,
                data=data,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.RequestError as exc:
            raise StripeError(f"network error: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise StripeError(
                f"stripe returned non-JSON ({resp.status_code})"
            ) from exc

        if resp.status_code >= 400:
            err = body.get("error") if isinstance(body, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(body)
            raise StripeError(
                f"stripe {path} failed ({resp.status_code}): {msg}"
            )
        if not isinstance(body, dict):
            raise StripeError(f"stripe {path} returned non-object body")
        return body


__all__ = ["PaymentLink", "StripeClient", "StripeError"]
