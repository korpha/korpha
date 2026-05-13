"""YAML config for MCP servers Korpha should connect to.

Default location: ``~/.korpha/mcp.yaml`` (override with
``KORPHA_MCP_FILE``). Schema:

```yaml
servers:
  - name: filesystem
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/me/docs"]
    env:
      LOG_LEVEL: info
    enabled: true                 # optional, default true
    request_timeout_seconds: 30   # optional

  - name: github
    command: ["mcp-server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}   # ${VAR} expansion
```

``${VAR}`` placeholders in env values are expanded from the parent
process environment so users can keep secrets out of the YAML.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from korpha.inference.limits import request_timeout

DEFAULT_CONFIG_PATH = Path.home() / ".korpha" / "mcp.yaml"


class McpConfigError(ValueError):
    """Malformed mcp.yaml or missing required fields."""


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    request_timeout_seconds: float = field(default_factory=request_timeout)


def config_path() -> Path:
    override = os.getenv("KORPHA_MCP_FILE")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_mcp_config(path: Path | None = None) -> list[McpServerConfig]:
    """Parse mcp.yaml. Returns ``[]`` when the file doesn't exist."""
    p = path or config_path()
    if not p.exists():
        return []

    import yaml

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise McpConfigError(f"{p}: top level must be a mapping")
    servers_raw = raw.get("servers")
    if not isinstance(servers_raw, list):
        raise McpConfigError(f"{p}: 'servers' must be a list")

    configs: list[McpServerConfig] = []
    seen: set[str] = set()
    for idx, entry in enumerate(servers_raw):
        cfg = _parse_entry(entry, source=p, index=idx)
        if cfg.name in seen:
            raise McpConfigError(
                f"{p}: duplicate server name {cfg.name!r}"
            )
        seen.add(cfg.name)
        configs.append(cfg)
    return configs


def _parse_entry(entry: Any, *, source: Path, index: int) -> McpServerConfig:
    if not isinstance(entry, dict):
        raise McpConfigError(
            f"{source}: servers[{index}] must be a mapping"
        )
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise McpConfigError(
            f"{source}: servers[{index}] missing required 'name'"
        )
    command_raw = entry.get("command")
    if isinstance(command_raw, str):
        command = command_raw.split()
    elif isinstance(command_raw, list):
        command = [str(c) for c in command_raw]
    else:
        raise McpConfigError(
            f"{source}: servers[{index}] 'command' must be a string or list"
        )
    if not command:
        raise McpConfigError(
            f"{source}: servers[{index}] 'command' is empty"
        )

    env_raw = entry.get("env") or {}
    if not isinstance(env_raw, dict):
        raise McpConfigError(
            f"{source}: servers[{index}] 'env' must be a mapping"
        )
    env: dict[str, str] = {}
    for k, v in env_raw.items():
        env[str(k)] = _expand_env_vars(str(v))

    cwd_raw = entry.get("cwd")
    cwd = str(cwd_raw) if cwd_raw is not None else None

    return McpServerConfig(
        name=str(name).strip(),
        command=command,
        env=env,
        cwd=cwd,
        enabled=bool(entry.get("enabled", True)),
        request_timeout_seconds=float(entry.get("request_timeout_seconds", 30.0)),
    )


_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(value: str) -> str:
    """Expand ``${VAR}`` placeholders from os.environ. Missing vars become
    empty strings (with a quiet trace) rather than raising — that mirrors
    shell behavior and lets users layer in keys later without re-parsing
    the config."""

    def _replace(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return _VAR_PATTERN.sub(_replace, value)


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "McpConfigError",
    "McpServerConfig",
    "config_path",
    "load_mcp_config",
]
