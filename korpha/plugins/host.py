"""PluginHost — capability-gated API surface handed to each plugin.

A plugin's ``register(host)`` function receives one of these. Methods
the manifest didn't declare permission for raise ``PluginPermissionError``
*before* any side effect happens, so a misbehaving plugin can't quietly
bypass its declared capabilities.

Capabilities currently understood:

  - ``skills``                 — register Skill instances with a SkillRegistry
  - ``wakeup_handlers``        — register heartbeat handlers
  - ``mcp_servers``            — declare additional MCP servers
  - ``channel_adapters``       — register a ChannelAdapter factory
  - ``inference_providers``    — register a ProviderProfile

Future capabilities (db_read, db_write, fs_read, fs_write, network)
will plug into the same gate without changing the plugin contract.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from korpha.channels.registry import (
    PlatformEntry,
    platform_registry,
)
from korpha.heartbeats.dispatcher import HandlerContext, HandlerRegistry
from korpha.inference.provider_profile import (
    ProviderProfile,
    provider_profile_registry,
)
from korpha.mcp.config import McpServerConfig
from korpha.skills.registry import SkillRegistry
from korpha.skills.types import Skill


class PluginPermissionError(PermissionError):
    """Plugin tried to use a capability its manifest didn't declare."""


HandlerFn = Callable[[HandlerContext], Awaitable[None]]


@dataclass
class PluginHost:
    """Capability-gated facade given to each plugin's ``register()``."""

    plugin_name: str
    permissions: frozenset[str]
    skill_registry: SkillRegistry
    handler_registry: HandlerRegistry
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    """Plugins append config entries here. The host process picks them up
    when initializing the MCP layer (so the plugin doesn't have to manage
    subprocess lifecycle itself)."""

    # Track everything the plugin actually contributed so we can attribute /
    # unregister later if needed.
    contributed_skills: list[str] = field(default_factory=list)
    contributed_handlers: list[str] = field(default_factory=list)
    contributed_mcp: list[str] = field(default_factory=list)
    contributed_channels: list[str] = field(default_factory=list)
    contributed_providers: list[str] = field(default_factory=list)
    contributed_hooks: list[str] = field(default_factory=list)
    contributed_memory_providers: list[str] = field(default_factory=list)

    def add_skill(self, skill: Skill) -> None:
        """Register a Skill on the shared registry. Requires ``skills``."""
        self._require("skills")
        self.skill_registry.add(skill)
        self.contributed_skills.append(skill.spec.name)

    def add_wakeup_handler(self, kind: str, fn: HandlerFn) -> None:
        """Register a heartbeat handler. Requires ``wakeup_handlers``."""
        self._require("wakeup_handlers")
        self.handler_registry.register(kind, fn)
        self.contributed_handlers.append(kind)

    def add_mcp_server(self, server: McpServerConfig) -> None:
        """Declare an MCP server for the host to spawn. Requires ``mcp_servers``."""
        self._require("mcp_servers")
        self.mcp_servers.append(server)
        self.contributed_mcp.append(server.name)

    def add_channel_adapter(self, entry: PlatformEntry) -> None:
        """Register a channel adapter (Telegram, Discord, Teams, …).
        Requires ``channel_adapters``. The host stamps ``source`` and
        ``plugin_name`` so dashboards can attribute the channel."""
        self._require("channel_adapters")
        # Stamp provenance so the host can show "channel X provided by
        # plugin Y" in the dashboard / setup CLI.
        entry.source = "plugin"
        entry.plugin_name = self.plugin_name
        platform_registry.register(entry)
        self.contributed_channels.append(entry.name)

    def add_inference_provider(self, profile: ProviderProfile) -> None:
        """Register an inference-provider profile (LLM backend).
        Requires ``inference_providers``. The host stamps ``source`` +
        ``plugin_name`` for provenance."""
        self._require("inference_providers")
        profile.source = "plugin"
        profile.plugin_name = self.plugin_name
        provider_profile_registry.register(profile)
        self.contributed_providers.append(profile.name)

    def add_long_term_memory(self, provider: object) -> None:
        """Register a long-term memory provider. Requires
        ``long_term_memory``. Single-active semantics — last
        registered plugin wins. Plugins should declare this
        capability in their manifest."""
        self._require("long_term_memory")
        from korpha.memory import LongTermMemory, set_active_provider
        if not isinstance(provider, LongTermMemory):
            raise TypeError(
                f"add_long_term_memory: provider must inherit "
                f"LongTermMemory, got {type(provider).__name__}"
            )
        set_active_provider(provider, plugin_name=self.plugin_name)
        self.contributed_memory_providers.append(provider.name)

    def add_lifecycle_hook(self, kind: str, fn: object) -> None:
        """Register a lifecycle hook callback. Requires
        ``lifecycle_hooks``. Used for observability plugins (Langfuse,
        PostHog) and policy gates without patching core. ``kind`` is
        one of ``pre_skill_call`` / ``post_skill_call`` /
        ``session_start`` / ``session_end``."""
        self._require("lifecycle_hooks")
        from korpha.plugins.hooks import HookKind, hook_registry
        try:
            hk = HookKind(kind)
        except ValueError as exc:
            valid = ", ".join(k.value for k in HookKind)
            raise ValueError(
                f"unknown hook kind {kind!r}; valid: {valid}"
            ) from exc
        hook_registry.register(
            hk, fn, plugin_name=self.plugin_name,  # type: ignore[arg-type]
        )
        self.contributed_hooks.append(f"{kind}:{self.plugin_name}")

    def has_permission(self, capability: str) -> bool:
        return capability in self.permissions

    def _require(self, capability: str) -> None:
        if capability not in self.permissions:
            raise PluginPermissionError(
                f"Plugin {self.plugin_name!r} tried to use {capability!r} "
                f"but its manifest declares only: {sorted(self.permissions)}"
            )


__all__ = ["PluginHost", "PluginPermissionError"]
