"""MCP (Model Context Protocol) client integration.

Lets Korpha connect to any MCP server — filesystem, GitHub, Postgres,
Notion, etc. — and surface their tools to skills / agents.

Today: stdio transport only. SSE / WebSocket transports can plug in
later behind the same ``McpClient`` protocol.
"""
from korpha.mcp.client import (
    McpClient,
    McpClientError,
    McpToolCallResult,
    McpToolDescriptor,
    StdioMcpClient,
)
from korpha.mcp.config import (
    McpConfigError,
    McpServerConfig,
    load_mcp_config,
)

__all__ = [
    "McpClient",
    "McpClientError",
    "McpConfigError",
    "McpServerConfig",
    "McpToolCallResult",
    "McpToolDescriptor",
    "StdioMcpClient",
    "load_mcp_config",
]
