#!/usr/bin/env python3
"""Tiny synchronous JSON-RPC 2.0 server speaking a subset of MCP over stdio.

Used only by the test suite to exercise StdioMcpClient without needing
npx / Node / a real MCP server installed. Implements:

  - initialize        (returns serverInfo + capabilities)
  - notifications/initialized  (acked silently)
  - tools/list        (returns one fixed tool)
  - tools/call        (echoes back as a text content block)
  - shutdown          (exits the process)

Failure modes for testing:
  - if MOCK_MCP_FAIL_INIT=1, return an error to initialize
  - if MOCK_MCP_TOOL_ERROR=1, return is_error=true to tools/call
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

TOOLS = [
    {
        "name": "echo",
        "description": "Echoes the input back as text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Adds two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def _send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle(msg: dict[str, Any]) -> None:
    method = msg.get("method")
    rpc_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        if os.getenv("MOCK_MCP_FAIL_INIT") == "1":
            _send({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -1, "message": "init forbidden"}})
            return
        _send(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "mock-mcp", "version": "0.0.1"},
                    "capabilities": {"tools": {}},
                },
            }
        )
        return

    if method == "notifications/initialized":
        return  # no response expected

    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        if os.getenv("MOCK_MCP_TOOL_ERROR") == "1":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [{"type": "text", "text": "tool failed"}],
                        "isError": True,
                    },
                }
            )
            return
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            text = str(args.get("text", ""))
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"content": [{"type": "text", "text": f"echo:{text}"}]},
                }
            )
            return
        if name == "add":
            a = float(args.get("a", 0))
            b = float(args.get("b", 0))
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"content": [{"type": "text", "text": str(a + b)}]},
                }
            )
            return
        _send(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"unknown tool {name!r}"},
            }
        )
        return

    if method == "shutdown":
        _send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
        sys.exit(0)

    if rpc_id is not None:
        _send(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"},
            }
        )


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        _handle(msg)


if __name__ == "__main__":
    main()
