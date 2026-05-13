"""Channel adapter registry — discovery + factory + capability metadata.

Built-in adapters (Telegram, email) and plugin-supplied adapters
register themselves here so the channel runtime never needs an
if/elif on platform name. Adding a new channel = ``register(...)``,
no edits to core.

Inspired by Hermes' ``gateway/platform_registry.py``. Stripped to
the fields Korpha actually needs today; extra metadata can be
added as feature flags require.

Plugin side:

    from korpha.channels.registry import platform_registry, PlatformEntry

    platform_registry.register(PlatformEntry(
        name="matrix",
        label="Matrix",
        adapter_factory=lambda cfg: MatrixAdapter(cfg),
        check_fn=lambda: importlib.util.find_spec("matrix_nio") is not None,
        required_env=["MATRIX_HOMESERVER", "MATRIX_USER_TOKEN"],
        install_hint="pip install matrix-nio",
        source="plugin",
        plugin_name="channel-matrix",
    ))

Runtime side:

    adapter = platform_registry.create_adapter("matrix", channel_config)
    if adapter is None:
        # registry returns None on missing deps / bad config — caller
        # decides whether to skip silently or surface to the user
        ...

We deliberately ship adapters only for protocols that explicitly
permit programmatic clients (Telegram bots, IMAP/SMTP, Matrix,
etc.). Adapters for platforms whose ToS prohibits automation
(Teams, LinkedIn, Facebook, X, Instagram) are not bundled — but
the contract above is open, so a founder who accepts the ToS
risk can author one as a personal plugin.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PlatformEntry:
    """Metadata + factory for one channel adapter.

    The shape mirrors Hermes' PlatformEntry (gateway/platform_registry.py)
    but trimmed to the fields Korpha actively reads. Add new fields
    here when a feature needs them rather than passing dicts through —
    the dataclass is the contract.
    """

    name: str
    """Stable identifier matching ``ThreadPlatform`` values
    (e.g. ``"telegram"``, ``"email"``, ``"teams"``). Used in config
    files + DB rows."""

    label: str
    """Human-readable label, e.g. "Microsoft Teams". Shown in CLI
    setup + dashboard."""

    adapter_factory: Callable[[Any], Any]
    """``(channel_config) -> ChannelAdapter``. Factory rather than a
    bare class so plugins can do custom init (extra kwargs, retry
    wrapping, dep injection)."""

    check_fn: Callable[[], bool] = field(default=lambda: True)
    """Returns True when the platform's dependencies are importable
    and the runtime can instantiate an adapter. Default ``True`` is
    fine for adapters that only need stdlib."""

    validate_config: Callable[[Any], bool] | None = None
    """Optional. ``(channel_config) -> bool``. If None, the registry
    skips config validation and lets the adapter fail at connect()
    time with a descriptive error."""

    required_env: list[str] = field(default_factory=list)
    """Env var names this adapter needs. Surfaced in ``korpha
    doctor`` and the interactive setup CLI."""

    install_hint: str = ""
    """Shown when ``check_fn`` returns False (e.g. ``pip install
    playwright``)."""

    setup_fn: Callable[[], None] | None = None
    """Optional interactive setup function — collects + persists
    env vars / config. Falls back to a generic ``set these env
    vars`` display when None. Mike-non-technical rule: every
    plugin-supplied adapter should ship one of these so users
    never edit YAML by hand."""

    source: str = "plugin"
    """``"builtin"`` or ``"plugin"``. Used by the dashboard to
    distinguish trust sources + by tests to filter."""

    plugin_name: str = ""
    """Manifest name of the plugin that registered this entry.
    Empty for built-ins. Used by ``korpha plugins enable
    <name>`` so the system can re-enable the right plugin when
    a user configures one of its platforms."""

    emoji: str = "🔌"
    """Display glyph for CLI / dashboard listings."""

    platform_hint: str = ""
    """Optional system-prompt nudge injected when the agent is
    speaking on this platform (e.g. ``"You are on IRC. Do not use
    markdown."``). Empty = no hint."""


class PlatformRegistry:
    """Central registry of channel adapters.

    Thread-safe for reads — dict lookups are atomic under the GIL.
    Writes happen at startup during sequential plugin discovery.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PlatformEntry] = {}

    def register(self, entry: PlatformEntry) -> None:
        """Register a platform adapter. Last writer wins on name
        collision so plugins can override built-ins when explicitly
        desired (uncommon)."""
        if entry.name in self._entries:
            prev = self._entries[entry.name]
            logger.info(
                "Platform '%s' re-registered (was %s, now %s)",
                entry.name, prev.source, entry.source,
            )
        self._entries[entry.name] = entry
        logger.debug(
            "Registered channel adapter: %s (%s)", entry.name, entry.source
        )

    def unregister(self, name: str) -> bool:
        """Remove an entry. Returns True if it existed."""
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> PlatformEntry | None:
        return self._entries.get(name)

    def all_entries(self) -> list[PlatformEntry]:
        return list(self._entries.values())

    def builtin_entries(self) -> list[PlatformEntry]:
        return [e for e in self._entries.values() if e.source == "builtin"]

    def plugin_entries(self) -> list[PlatformEntry]:
        return [e for e in self._entries.values() if e.source == "plugin"]

    def is_registered(self, name: str) -> bool:
        return name in self._entries

    def create_adapter(self, name: str, config: Any) -> Any | None:
        """Create an adapter instance for ``name`` against ``config``.

        Returns ``None`` when:
          - No entry is registered for ``name``
          - ``check_fn()`` returns False (missing deps — logs a
            warning + the install hint)
          - ``validate_config()`` returns False (misconfigured)
          - The factory raises (logs the exception)

        Returns the adapter instance when everything succeeds.
        Caller is responsible for calling ``connect()`` / ``stream()``.
        """
        entry = self._entries.get(name)
        if entry is None:
            return None

        if not entry.check_fn():
            hint = f" ({entry.install_hint})" if entry.install_hint else ""
            logger.warning(
                "Channel '%s' requirements not met%s", entry.label, hint
            )
            return None

        if entry.validate_config is not None:
            try:
                if not entry.validate_config(config):
                    logger.warning(
                        "Channel '%s' config validation failed", entry.label
                    )
                    return None
            except Exception as exc:
                logger.warning(
                    "Channel '%s' config validation raised: %s",
                    entry.label, exc,
                )
                return None

        try:
            return entry.adapter_factory(config)
        except Exception as exc:
            logger.error(
                "Failed to construct adapter for channel '%s': %s",
                entry.label, exc, exc_info=True,
            )
            return None


# Module-level singleton. Importing this module + calling .register()
# is the public registration surface.
platform_registry = PlatformRegistry()


__all__ = [
    "PlatformEntry",
    "PlatformRegistry",
    "platform_registry",
]
