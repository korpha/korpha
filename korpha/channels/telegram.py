"""Telegram bot adapter using long-polling.

Why long polling not webhooks: the OSS install is single-tenant on a
laptop / home server with no public IP. Webhooks need an HTTPS URL the
internet can reach. Long polling works behind any NAT, makes setup
zero-friction, and Telegram explicitly supports it for low-volume bots.

Bot lifecycle:

  1. Constructed with the token from @BotFather.
  2. ``stream()`` issues ``getUpdates`` with a 25s long-poll timeout in a
     loop, yielding IncomingMessage for each text message that arrives.
     Updates are cursor-based via the ``offset`` parameter so we never
     re-process the same message twice.
  3. ``send()`` posts back via the ``sendMessage`` API.
  4. ``close()`` aborts in-flight requests and closes the httpx client.

Allowlist semantics:
  - Pass ``allowed_chat_ids`` (set of int) to restrict who the bot will
    process. Empty set = process anyone (lab mode). Production should
    always set this.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from korpha.channels.base import (
    ChannelAdapter,
    ChannelError,
    IncomingMessage,
    OutgoingMessage,
)
from korpha.cofounder.model import ThreadPlatform

_API_ROOT = "https://api.telegram.org"
_LONG_POLL_TIMEOUT_SECONDS = 25


@dataclass
class TelegramAdapter(ChannelAdapter):
    """Telegram channel adapter (long-polling)."""

    token: str
    """Bot token issued by @BotFather. NEVER log this — treat like a password."""

    allowed_chat_ids: set[int] = field(default_factory=set)
    """If non-empty, only messages from these chat_ids are surfaced. Empty
    means "no allowlist enforced" — fine for testing, dangerous in prod."""

    api_base: str = _API_ROOT
    """Override for tests pointing at a mocked Telegram server."""

    platform: ThreadPlatform = ThreadPlatform.TELEGRAM

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _last_offset: int = field(default=0, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=_LONG_POLL_TIMEOUT_SECONDS + 10,
                    write=10.0,
                    pool=5.0,
                )
            )
        return self._client

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    async def get_me(self) -> dict[str, Any]:
        """Round-trip ``getMe`` — useful as a health-check during setup."""
        resp = await self._http().get(self._url("getMe"))
        data = _check_ok(resp, method="getMe")
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    async def stream(self) -> AsyncIterator[IncomingMessage]:
        """Yield incoming text messages forever. Cancel the consuming task
        to stop the stream cleanly; the next iteration sees ``_closed``
        and exits."""
        while not self._closed:
            try:
                updates = await self._poll_updates()
            except (httpx.RequestError, ChannelError) as exc:
                # Network blip — back off briefly so we don't hammer the API
                # if Telegram is having a bad day.
                if self._closed:
                    return
                await asyncio.sleep(2.0)
                _ = exc
                continue
            for update in updates:
                update_id = int(update.get("update_id", 0))
                if update_id >= self._last_offset:
                    self._last_offset = update_id + 1
                msg = update.get("message")
                if not isinstance(msg, dict):
                    continue
                text = msg.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue  # ignore non-text (photos, stickers, etc.) for now
                chat = msg.get("chat") or {}
                chat_id = int(chat.get("id", 0))
                if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
                    continue
                from_user = msg.get("from") or {}
                display = (
                    from_user.get("first_name")
                    or from_user.get("username")
                    or str(chat_id)
                )
                yield IncomingMessage(
                    platform=self.platform,
                    channel_user_id=str(chat_id),
                    text=text,
                    display_name=str(display),
                    raw=update,
                )

    async def _poll_updates(self) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "timeout": _LONG_POLL_TIMEOUT_SECONDS,
            "offset": self._last_offset,
            "allowed_updates": "message",
        }
        resp = await self._http().get(self._url("getUpdates"), params=params)
        data = _check_ok(resp, method="getUpdates")
        result = data.get("result")
        if not isinstance(result, list):
            return []
        return [r for r in result if isinstance(r, dict)]

    async def send(self, message: OutgoingMessage) -> None:
        if not message.text.strip():
            return
        payload = {
            "chat_id": message.channel_user_id,
            "text": message.text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        resp = await self._http().post(self._url("sendMessage"), json=payload)
        # Telegram rejects malformed Markdown — fall back to plain text so the
        # Founder always sees the message even if formatting was off.
        if resp.status_code == 400:
            payload.pop("parse_mode", None)
            resp = await self._http().post(self._url("sendMessage"), json=payload)
        _check_ok(resp, method="sendMessage")

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _check_ok(resp: httpx.Response, *, method: str) -> dict[str, Any]:
    """Raise ChannelError on non-2xx or Telegram ``ok: false``."""
    if resp.status_code >= 500:
        raise ChannelError(
            f"telegram {method} returned {resp.status_code} — server unhappy"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise ChannelError(
            f"telegram {method} returned non-JSON ({resp.status_code})"
        ) from exc
    if not isinstance(data, dict):
        raise ChannelError(f"telegram {method} returned non-object body")
    if not data.get("ok"):
        desc = data.get("description") or "(no description)"
        raise ChannelError(f"telegram {method} not ok: {desc}")
    return data


__all__ = ["TelegramAdapter"]


# ---------------------------------------------------------------------------
# Self-register with the platform registry — built-ins go in alongside
# plugin-supplied adapters. Importing this module is the registration.
# ---------------------------------------------------------------------------


def _register_telegram() -> None:
    from korpha.channels.registry import (
        PlatformEntry,
        platform_registry,
    )

    def _factory(cfg: Any) -> TelegramAdapter:
        token = getattr(cfg, "token", None) or (
            cfg.get("token") if isinstance(cfg, dict) else None
        )
        allowed = getattr(cfg, "allowed_chat_ids", None) or (
            cfg.get("allowed_chat_ids")
            if isinstance(cfg, dict) else set()
        )
        return TelegramAdapter(
            token=str(token or ""), allowed_chat_ids=set(allowed or set()),
        )

    def _validate(cfg: Any) -> bool:
        token = getattr(cfg, "token", None) or (
            cfg.get("token") if isinstance(cfg, dict) else None
        )
        return bool(token)

    platform_registry.register(PlatformEntry(
        name=ThreadPlatform.TELEGRAM.value,
        label="Telegram",
        adapter_factory=_factory,
        check_fn=lambda: True,  # httpx is a hard dep of Korpha core
        validate_config=_validate,
        required_env=["KORPHA_TELEGRAM_TOKEN"],
        install_hint="",
        source="builtin",
        emoji="✈️",
    ))


_register_telegram()
