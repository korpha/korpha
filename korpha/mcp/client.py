"""Minimal MCP client: JSON-RPC 2.0 over stdio.

We deliberately do NOT depend on the official ``mcp`` Python SDK. The
protocol surface Korpha needs today (initialize, tools/list,
tools/call) is small enough that vendoring the SDK adds more risk than
value, and a hand-rolled client lets us keep tight control over
timeouts, cancellation, and how tool results are surfaced to skills.

Lifecycle:

    client = StdioMcpClient(...)
    async with client:
        tools = await client.list_tools()
        result = await client.call_tool(name, arguments)

The background reader task pumps stdout into a per-request future
keyed on the JSON-RPC ``id``. Stderr is captured to an internal buffer
so subprocess crashes can include diagnostic output in the raised error.

Notifications (server → client requests like ``logging/log``) are
discarded today. A subclass can override ``_handle_notification`` once
we want to surface them to the agent layer.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import count
from types import TracebackType
from typing import Any, Protocol

from korpha.inference.limits import request_timeout


class McpClientError(RuntimeError):
    """Server returned an error, the subprocess died, or a request timed out."""


@dataclass(frozen=True)
class McpToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]
    """JSON-Schema describing the arguments. Pass straight through to a model
    that supports OpenAI tool-calling."""


@dataclass(frozen=True)
class McpToolCallResult:
    content: list[dict[str, Any]]
    """List of content blocks per the MCP spec: each has ``type`` (text,
    image, resource) and a payload field. Most servers return one text block."""

    is_error: bool = False
    """True when the server reported a tool-level error in the response.
    Caller decides how to surface it — skills typically raise."""

    def text(self) -> str:
        """Concatenate every text content block. Convenience for callers that
        don't care about images / resources."""
        parts: list[str] = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)


class McpClient(Protocol):
    """Transport-agnostic MCP client surface. Today only StdioMcpClient
    implements it; SSE / WebSocket can join later."""

    async def initialize(self) -> None: ...
    async def list_tools(self) -> list[McpToolDescriptor]: ...
    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> McpToolCallResult: ...
    async def close(self) -> None: ...


_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "korpha", "version": "0.0.1"}


@dataclass
class StdioMcpClient:
    """MCP client that talks to a server via its stdin/stdout."""

    command: list[str]
    """argv. Example: ['npx', '-y', '@modelcontextprotocol/server-filesystem', '/tmp']"""

    env: Mapping[str, str] | None = None
    """Extra env vars merged on top of os.environ. Use for API tokens."""

    cwd: str | None = None
    request_timeout_seconds: float = field(default_factory=request_timeout)
    init_timeout_seconds: float = 15.0
    """Initial handshake timeout — kept short (15s) since failure here
    means the MCP binary won't start. Override per-instance only when
    using a slow-spawning server."""

    _process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stderr_buf: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _pending: dict[int, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _id_counter: count[int] = field(default_factory=lambda: count(1), init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)

    async def __aenter__(self) -> StdioMcpClient:
        await self._spawn()
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _spawn(self) -> None:
        if self._process is not None:
            return
        # Pre-flight: refuse known-malware packages from OSV. Mike will
        # add MCP servers that ChatGPT recommends — typo-squat / supply-
        # chain malware in npm is the realistic threat. Fail-open on
        # network errors so an offline install isn't blocked.
        from korpha.security import check_package_for_malware

        if self.command:
            verdict = check_package_for_malware(
                self.command[0], list(self.command[1:]),
            )
            if verdict:
                raise McpClientError(verdict)

        merged_env = os.environ.copy()
        if self.env:
            merged_env.update(self.env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
                cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise McpClientError(
                f"MCP server binary not found: {self.command[0]!r}"
            ) from exc
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-reader[{self.command[0]}]"
        )

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._process is None:
            await self._spawn()

        try:
            await asyncio.wait_for(
                self._call(
                    "initialize",
                    {
                        "protocolVersion": _PROTOCOL_VERSION,
                        "clientInfo": _CLIENT_INFO,
                        "capabilities": {},
                    },
                ),
                timeout=self.init_timeout_seconds,
            )
        except TimeoutError as exc:
            raise McpClientError(
                f"MCP server {self.command[0]!r} did not respond to "
                f"initialize within {self.init_timeout_seconds}s. "
                f"stderr: {self._stderr_text()[:300]}"
            ) from exc

        # Spec requires sending `notifications/initialized` after the
        # initialize handshake completes.
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def list_tools(self) -> list[McpToolDescriptor]:
        result = await self._call("tools/list", {})
        tools_raw = result.get("tools") or []
        out: list[McpToolDescriptor] = []
        for t in tools_raw:
            if not isinstance(t, dict):
                continue
            out.append(
                McpToolDescriptor(
                    name=str(t.get("name", "")),
                    description=str(t.get("description", "")),
                    input_schema=dict(t.get("inputSchema") or {}),
                )
            )
        return out

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> McpToolCallResult:
        result = await self._call(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        content = result.get("content") or []
        if not isinstance(content, list):
            content = []
        return McpToolCallResult(
            content=[c for c in content if isinstance(c, dict)],
            is_error=bool(result.get("isError")),
        )

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._process is not None:
            # Close stdin first so the server sees EOF and exits cleanly. This
            # also detaches the asyncio write transport so its __del__ doesn't
            # later try to call into a closed event loop.
            if self._process.stdin is not None:
                with contextlib.suppress(Exception):
                    self._process.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            if self._process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                with contextlib.suppress(Exception):
                    await self._process.wait()
            self._process = None
        # Fail any still-pending requests with a clear error so callers
        # don't hang waiting for a dead server.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpClientError("MCP client closed"))
        self._pending.clear()
        self._initialized = False

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._process is None:
            raise McpClientError("MCP client not started")
        rpc_id = next(self._id_counter)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = future
        await self._send(
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout_seconds)
        except TimeoutError as exc:
            self._pending.pop(rpc_id, None)
            raise McpClientError(
                f"MCP request {method!r} timed out after "
                f"{self.request_timeout_seconds}s"
            ) from exc

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise McpClientError("MCP server stdin is closed")
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._process.stdin.write(line)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpClientError(
                f"MCP server stdin pipe broken — server likely crashed. "
                f"stderr: {self._stderr_text()[:300]}"
            ) from exc

    async def _read_loop(self) -> None:
        assert self._process is not None
        stdout = self._process.stdout
        stderr = self._process.stderr
        if stdout is None:
            return
        # Drain stderr concurrently for diagnostics.
        stderr_task: asyncio.Task[None] | None = None
        if stderr is not None:
            stderr_task = asyncio.create_task(self._drain_stderr(stderr))
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stderr_task

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                return
            self._stderr_buf.extend(chunk)
            # Cap the buffer so a chatty server doesn't OOM us.
            if len(self._stderr_buf) > 64_000:
                del self._stderr_buf[: len(self._stderr_buf) - 64_000]

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            self._resolve(msg)
        else:
            # Notification or server-initiated request — discard for now.
            self._handle_notification(msg)

    def _resolve(self, msg: dict[str, Any]) -> None:
        rpc_id_raw = msg.get("id")
        try:
            rpc_id = int(rpc_id_raw) if rpc_id_raw is not None else None
        except (TypeError, ValueError):
            return
        if rpc_id is None:
            return
        future = self._pending.pop(rpc_id, None)
        if future is None or future.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            future.set_exception(
                McpClientError(
                    f"MCP error {err.get('code', '?')}: {err.get('message', '?')}"
                )
            )
            return
        result = msg.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        # Subclass hook; default is to drop.
        _ = msg

    def _stderr_text(self) -> str:
        return self._stderr_buf.decode("utf-8", errors="replace")


__all__ = [
    "McpClient",
    "McpClientError",
    "McpToolCallResult",
    "McpToolDescriptor",
    "StdioMcpClient",
]
