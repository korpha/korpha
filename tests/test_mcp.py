"""MCP client + config tests using a tiny in-process mock server."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from korpha.mcp import (
    McpClientError,
    McpConfigError,
    StdioMcpClient,
    load_mcp_config,
)
from korpha.mcp.config import _expand_env_vars

MOCK_SERVER = Path(__file__).resolve().parent / "fixtures" / "mock_mcp_server.py"


def _client(env: dict[str, str] | None = None) -> StdioMcpClient:
    return StdioMcpClient(
        command=[sys.executable, str(MOCK_SERVER)],
        env=env,
        request_timeout_seconds=5.0,
        init_timeout_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_initialize_and_list_tools() -> None:
    async with _client() as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {"echo", "add"}
    echo = next(t for t in tools if t.name == "echo")
    assert "Echoes" in echo.description
    assert echo.input_schema["properties"]["text"]["type"] == "string"


@pytest.mark.asyncio
async def test_call_tool_echo() -> None:
    async with _client() as client:
        result = await client.call_tool("echo", {"text": "hello"})
    assert result.is_error is False
    assert result.text() == "echo:hello"


@pytest.mark.asyncio
async def test_call_tool_add_returns_numeric() -> None:
    async with _client() as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})
    assert result.text() == "5.0"


@pytest.mark.asyncio
async def test_unknown_tool_raises() -> None:
    async with _client() as client:
        with pytest.raises(McpClientError) as exc:
            await client.call_tool("does_not_exist", {})
    assert "unknown tool" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_tool_error_propagates_as_is_error() -> None:
    """Server returning isError=true should surface on the result, not raise."""
    async with _client(env={"MOCK_MCP_TOOL_ERROR": "1"}) as client:
        result = await client.call_tool("echo", {"text": "x"})
    assert result.is_error is True
    assert "tool failed" in result.text()


@pytest.mark.asyncio
async def test_init_failure_raises() -> None:
    client = _client(env={"MOCK_MCP_FAIL_INIT": "1"})
    with pytest.raises(McpClientError):
        async with client:
            pass


@pytest.mark.asyncio
async def test_missing_binary_raises_clear_error() -> None:
    client = StdioMcpClient(command=["/this/binary/does/not/exist"])
    with pytest.raises(McpClientError) as exc:
        async with client:
            pass
    assert "not found" in str(exc.value).lower()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
async def test_close_unblocks_pending_requests() -> None:
    """After close(), subsequent calls must raise instead of hanging.

    The unraisable-warning filter is for asyncio's BaseSubprocessTransport
    __del__ running after the test loop closes — harmless in this test."""
    client = _client()
    await client._spawn()
    await client.initialize()
    await client.close()
    with pytest.raises(McpClientError):
        await client.list_tools()


# ─────── config tests ───────


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_mcp_config(tmp_path / "missing.yaml") == []


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        """
servers:
  - name: filesystem
    command: ["mcp-fs", "/home/me"]
""",
        encoding="utf-8",
    )
    [server] = load_mcp_config(cfg)
    assert server.name == "filesystem"
    assert server.command == ["mcp-fs", "/home/me"]
    assert server.enabled is True


def test_command_string_is_split(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        """
servers:
  - name: fs
    command: "mcp-fs /tmp"
""",
        encoding="utf-8",
    )
    [server] = load_mcp_config(cfg)
    assert server.command == ["mcp-fs", "/tmp"]


def test_env_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        """
servers:
  - name: gh
    command: ["mcp-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}
      LOG_LEVEL: info
""",
        encoding="utf-8",
    )
    [server] = load_mcp_config(cfg)
    assert server.env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_secret"
    assert server.env["LOG_LEVEL"] == "info"


def test_duplicate_name_errors(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        """
servers:
  - name: same
    command: ["a"]
  - name: same
    command: ["b"]
""",
        encoding="utf-8",
    )
    with pytest.raises(McpConfigError):
        load_mcp_config(cfg)


def test_missing_command_errors(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        """
servers:
  - name: x
""",
        encoding="utf-8",
    )
    with pytest.raises(McpConfigError):
        load_mcp_config(cfg)


def test_expand_env_vars_missing_becomes_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOESNT_EXIST_XYZ", raising=False)
    assert _expand_env_vars("a${DOESNT_EXIST_XYZ}b") == "ab"
