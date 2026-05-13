"""Email notifiers: Resend (production) + Mock (tests).

Resend was chosen because:
  - Modern HTTP API, no SMTP daemon needed
  - Reasonable free tier for solo operators
  - Domain verification flow is simple
  - Good deliverability defaults

The ``Notifier`` ABC keeps SMTP / Postmark / Mailgun additions to a
single class — no switch-case in calling code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from korpha.notifications.base import (
    Notification,
    Notifier,
    NotifierError,
)


@dataclass
class ResendEmailNotifier(Notifier):
    """Send email via Resend's HTTP API (https://resend.com).

    Auth: ``RESEND_API_KEY`` env var (or ``api_key`` constructor arg).
    From: ``RESEND_FROM`` env var (or ``default_from`` arg). Resend
    requires the from-domain to be verified before delivery.
    """

    api_key: str | None = None
    """Falls back to RESEND_API_KEY env var. Required at send time."""

    default_from: str | None = None
    """Falls back to RESEND_FROM env var. Required at send time unless
    every Notification carries ``from_address``."""

    api_base: str = "https://api.resend.com"
    """Override for tests pointing at a mocked transport."""

    name: str = "resend"

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _resolve_api_key(self) -> str:
        key = self.api_key or os.getenv("RESEND_API_KEY")
        if not key:
            raise NotifierError(
                "RESEND_API_KEY not set. Get one from https://resend.com "
                "and add it to .env or your environment."
            )
        return key

    def _resolve_from(self, override: str | None) -> str:
        addr = override or self.default_from or os.getenv("RESEND_FROM")
        if not addr:
            raise NotifierError(
                "Resend requires a verified from-address. Set RESEND_FROM in "
                ".env (e.g. 'Korpha <bot@yourdomain.com>')."
            )
        return addr

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def send(self, notification: Notification) -> None:
        api_key = self._resolve_api_key()
        from_addr = self._resolve_from(notification.from_address)
        payload: dict[str, object] = {
            "from": from_addr,
            "to": [notification.to],
            "subject": notification.subject,
            "text": notification.text_body,
        }
        if notification.html_body:
            payload["html"] = notification.html_body

        try:
            resp = await self._http().post(
                f"{self.api_base}/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.RequestError as exc:
            raise NotifierError(f"resend network error: {exc}") from exc

        if resp.status_code >= 400:
            try:
                body = resp.json()
                detail = body.get("message") or body.get("error") or resp.text
            except ValueError:
                detail = resp.text
            raise NotifierError(
                f"resend returned {resp.status_code}: {str(detail)[:300]}"
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class MockEmailNotifier(Notifier):
    """Records every send into ``sent`` for assertion in tests. Never raises
    unless ``raise_with`` is set."""

    name: str = "mock-email"
    sent: list[Notification] = field(default_factory=list)
    raise_with: str | None = None

    async def send(self, notification: Notification) -> None:
        if self.raise_with is not None:
            raise NotifierError(self.raise_with)
        self.sent.append(notification)

    async def close(self) -> None:
        return None


__all__ = ["MockEmailNotifier", "ResendEmailNotifier"]
