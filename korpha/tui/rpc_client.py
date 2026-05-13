"""WebSocket JSON-RPC client for the TUI.

Connects to the Korpha server's ``/api/tui/ws`` route, multiplexes
requests + events on the same socket, and exposes a tidy async API:

    client = RpcClient("ws://localhost:8765/api/tui/ws")
    async with client:
        # one-shot request → response
        identity = await client.call("me", {})

        # streaming request → events flow into the listener
        client.on_event("content.delta", on_chunk)
        await client.call("prompt.submit", {"message": "hi"})

The client tracks pending requests by id and resolves them when the
server returns a response with a matching id. Events (no id) get
fanned out to subscribed callbacks. Disconnects raise ``RpcClosed``
on any in-flight request so the TUI can show a clean error rather
than hang.

Why not just ``aiohttp.ClientSession``: ``websockets`` is the
canonical async WS library, depended on by FastAPI for the server
side, and gives us cleaner cancellation semantics.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)


class RpcClientError(Exception):
    """Generic RPC client error — wraps server-side error responses."""

    def __init__(
        self, code: int, message: str, data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RpcClosed(RpcClientError):
    """Raised when the connection drops while a request is in flight,
    or when the user calls ``call()`` on a closed client."""

    def __init__(self, reason: str = "connection closed") -> None:
        super().__init__(code=-32099, message=reason)


EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class _Pending:
    future: asyncio.Future[Any]


@dataclass
class RpcClient:
    """One WS connection. Use as ``async with`` so connect / close
    is bracketed."""

    url: str
    timeout_seconds: float = 30.0
    """Per-call timeout when the caller passes none. ``call`` lets
    you override per-request — useful for long LLM calls."""

    _ws: WebSocketClientProtocol | None = field(default=None, init=False)
    _next_id: int = field(default=0, init=False)
    _pending: dict[Any, _Pending] = field(default_factory=dict, init=False)
    _event_handlers: dict[str, list[EventCallback]] = field(
        default_factory=dict, init=False,
    )
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)
    _ready_payload: dict[str, Any] | None = field(default=None, init=False)
    """The first ``gateway.ready`` event the server sends. Stashed
    so callers can inspect identity + method catalog without an
    extra round-trip."""

    _ready_event: asyncio.Event = field(
        default_factory=asyncio.Event, init=False,
    )

    async def __aenter__(self) -> "RpcClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the WS + start the reader task. Returns once the
        socket is open; ``ready_payload()`` blocks separately until
        ``gateway.ready`` arrives."""
        if self._ws is not None:
            return
        self._ws = await websockets.connect(self.url, max_size=8 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        # Fail any still-pending calls
        for p in self._pending.values():
            if not p.future.done():
                p.future.set_exception(RpcClosed())
        self._pending.clear()

    async def ready_payload(self, timeout: float | None = 5.0) -> dict[str, Any]:
        """Block until the server's ``gateway.ready`` event arrives.

        The TUI typically calls this right after connect to render
        identity in the status bar."""
        if self._ready_payload is not None:
            return self._ready_payload
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        if self._ready_payload is None:
            raise RpcClosed("never received gateway.ready event")
        return self._ready_payload

    def on_event(self, name: str, callback: EventCallback) -> None:
        """Subscribe to events with this method name. Multiple
        subscribers per name are allowed; we call them in registration
        order. Subscribers can be sync or async."""
        self._event_handlers.setdefault(name, []).append(callback)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a request, await the response. Raises ``RpcClientError``
        on server errors, ``RpcClosed`` if the socket drops mid-flight,
        ``asyncio.TimeoutError`` past the timeout."""
        if self._closed or self._ws is None:
            raise RpcClosed()

        self._next_id += 1
        request_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = _Pending(future=future)
        try:
            await self._ws.send(json.dumps(payload))
        except ConnectionClosed as exc:
            self._pending.pop(request_id, None)
            raise RpcClosed(str(exc)) from exc

        try:
            return await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self.timeout_seconds,
            )
        finally:
            self._pending.pop(request_id, None)

    async def call_no_wait_id(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> int:
        """Fire a request and return its id WITHOUT awaiting the
        response. Used by ``prompt.submit`` so the caller can register
        the id with prompt.interrupt before the response arrives.

        The caller is responsible for awaiting via ``wait_for_id``
        or processing events themselves."""
        if self._closed or self._ws is None:
            raise RpcClosed()

        self._next_id += 1
        request_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = _Pending(future=future)
        await self._ws.send(json.dumps(payload))
        return request_id

    async def wait_for_id(
        self, request_id: int, *, timeout: float | None = None,
    ) -> Any:
        """Companion to ``call_no_wait_id``."""
        pending = self._pending.get(request_id)
        if pending is None:
            raise RpcClosed(f"no pending call for id={request_id}")
        try:
            return await asyncio.wait_for(
                pending.future,
                timeout=timeout if timeout is not None else self.timeout_seconds,
            )
        finally:
            self._pending.pop(request_id, None)

    async def _reader_loop(self) -> None:
        try:
            assert self._ws is not None
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("RPC reader saw non-JSON frame: %r", raw[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                request_id = msg.get("id")
                if request_id is not None and (
                    "result" in msg or "error" in msg
                ):
                    pending = self._pending.get(request_id)
                    if pending is None or pending.future.done():
                        continue
                    if "error" in msg:
                        err = msg.get("error") or {}
                        pending.future.set_exception(
                            RpcClientError(
                                code=int(err.get("code", -32603)),
                                message=str(err.get("message", "")),
                                data=err.get("data"),
                            )
                        )
                    else:
                        pending.future.set_result(msg.get("result"))
                    continue
                # Otherwise it's an event (no id)
                method_name = msg.get("method")
                if not isinstance(method_name, str):
                    continue
                if method_name == "gateway.ready":
                    self._ready_payload = msg.get("params") or {}
                    self._ready_event.set()
                handlers = self._event_handlers.get(method_name) or []
                # Also fire wildcard subscribers if any want every event
                handlers = handlers + (self._event_handlers.get("*") or [])
                for cb in handlers:
                    try:
                        result = cb({
                            "method": method_name,
                            "params": msg.get("params") or {},
                        })
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception(
                            "RPC event handler for %s raised", method_name,
                        )
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("RPC reader loop crashed")
        finally:
            # If the reader exits we treat the connection as dead;
            # fail any still-pending calls.
            for p in self._pending.values():
                if not p.future.done():
                    p.future.set_exception(RpcClosed())
            self._pending.clear()


__all__ = [
    "EventCallback",
    "RpcClient",
    "RpcClientError",
    "RpcClosed",
]
