"""Channel adapter + router tests using a stubbed Telegram backend."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from korpha.channels import (
    ChannelAdapter,
    ChannelError,
    IncomingMessage,
    OutgoingMessage,
    TelegramAdapter,
)
from korpha.channels.router import platform_from_name
from korpha.cofounder.model import ThreadPlatform


def _telegram_handler(updates: list[dict], sent: list[dict]) -> httpx.MockTransport:
    """httpx mock transport that emulates the two Telegram methods we use."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getUpdates"):
            # Pop one batch then return empty so the loop hangs on re-poll.
            batch = list(updates)  # copy before clearing to avoid aliasing
            updates.clear()
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/sendMessage"):
            sent.append(httpx.QueryParams(request.content.decode("utf-8") or "").get(""))
            try:
                import json as _json
                payload = _json.loads(request.content.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            sent[-1] = payload  # type: ignore[index]
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 1, "username": "test_bot"}},
            )
        return httpx.Response(404, json={"ok": False, "description": "no"})

    return httpx.MockTransport(handler)


def _adapter_with_transport(
    updates: list[dict], sent: list[dict], **kwargs
) -> TelegramAdapter:
    adapter = TelegramAdapter(token="t", **kwargs)
    # Inject the mock transport. This is private but lets us avoid spinning
    # up a real httpx.AsyncClient behind a real network.
    adapter._client = httpx.AsyncClient(
        transport=_telegram_handler(updates, sent),
        timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0),
    )
    return adapter


def _update(chat_id: int, text: str, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Mike"},
            "text": text,
            "date": 0,
        },
    }


@pytest.mark.asyncio
async def test_get_me_roundtrip() -> None:
    sent: list[dict] = []
    adapter = _adapter_with_transport([], sent)
    me = await adapter.get_me()
    assert me["username"] == "test_bot"
    await adapter.close()


@pytest.mark.asyncio
async def test_stream_yields_text_messages() -> None:
    updates = [_update(42, "hello cofounder", 1)]
    sent: list[dict] = []
    adapter = _adapter_with_transport(updates, sent)
    received: list[IncomingMessage] = []

    gen = adapter.stream()
    try:
        msg = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        received.append(msg)
    finally:
        await gen.aclose()
    await adapter.close()

    assert len(received) == 1
    assert received[0].platform == ThreadPlatform.TELEGRAM
    assert received[0].channel_user_id == "42"
    assert received[0].text == "hello cofounder"
    assert received[0].display_name == "Mike"


@pytest.mark.asyncio
async def test_stream_skips_non_text_updates() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 1}, "photo": [{}]}},  # no text
        _update(1, "real message", 2),
    ]
    sent: list[dict] = []
    adapter = _adapter_with_transport(updates, sent)
    gen = adapter.stream()
    try:
        msg = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    finally:
        await gen.aclose()
    await adapter.close()
    assert msg.text == "real message"


@pytest.mark.asyncio
async def test_allowlist_filters_strangers() -> None:
    updates = [_update(99, "spam", 1), _update(42, "legit", 2)]
    sent: list[dict] = []
    adapter = _adapter_with_transport(updates, sent, allowed_chat_ids={42})
    gen = adapter.stream()
    try:
        msg = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    finally:
        await gen.aclose()
    await adapter.close()
    assert msg.channel_user_id == "42"


@pytest.mark.asyncio
async def test_send_posts_markdown_payload() -> None:
    sent: list[dict] = []
    adapter = _adapter_with_transport([], sent)
    await adapter.send(OutgoingMessage(channel_user_id="42", text="*bold*"))
    await adapter.close()
    assert sent[-1]["chat_id"] == "42"
    assert sent[-1]["text"] == "*bold*"
    assert sent[-1]["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_send_skips_empty_text() -> None:
    sent: list[dict] = []
    adapter = _adapter_with_transport([], sent)
    await adapter.send(OutgoingMessage(channel_user_id="42", text="   "))
    await adapter.close()
    assert sent == []


@pytest.mark.asyncio
async def test_send_falls_back_to_plaintext_on_400() -> None:
    """Telegram rejects malformed Markdown with 400; we should retry plain."""
    attempts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.content.decode("utf-8") or "{}")
        attempts.append(body)
        if "parse_mode" in body:
            return httpx.Response(
                400,
                json={"ok": False, "description": "can't parse entities"},
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    adapter = TelegramAdapter(token="t")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await adapter.send(
        OutgoingMessage(channel_user_id="42", text="weird **markdown* [")
    )
    await adapter.close()
    assert len(attempts) == 2
    assert "parse_mode" in attempts[0]
    assert "parse_mode" not in attempts[1]


@pytest.mark.asyncio
async def test_telegram_not_ok_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "description": "Bad Request: chat not found"},
        )

    adapter = TelegramAdapter(token="t")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ChannelError) as exc:
        await adapter.send(OutgoingMessage(channel_user_id="42", text="hi"))
    assert "chat not found" in str(exc.value)
    await adapter.close()


def test_platform_from_name_maps() -> None:
    assert platform_from_name("telegram") == ThreadPlatform.TELEGRAM
    assert platform_from_name("DISCORD") == ThreadPlatform.DISCORD
    with pytest.raises(ValueError):
        platform_from_name("carrier-pigeon")


def test_channel_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        ChannelAdapter()  # type: ignore[abstract]
