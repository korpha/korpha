"""Plugin system: community-shippable capabilities.

A plugin is a directory under ``~/.korpha/plugins/`` (or
``KORPHA_PLUGINS_DIR``) containing a ``plugin.yaml`` manifest and an
entry-point Python module. The loader reads the manifest, imports the
module, and calls its ``register(host)`` function. The host hands the
plugin a capability-gated API surface so it can only do what its
manifest declared.

Today plugins run **in-process** — same Python interpreter as
Korpha. That's fine for trusted contributors and self-hosted users
but is NOT safe for arbitrary third-party plugins. Out-of-process
sandboxing (subprocess + IPC) is future work; the manifest already
carries the permission set so adding a sandbox doesn't change the
plugin contract.
"""
from korpha.plugins.host import PluginHost, PluginPermissionError
from korpha.plugins.loader import (
    PluginLoadError,
    PluginManifest,
    discover_plugins,
    load_plugin,
)

__all__ = [
    "PluginHost",
    "PluginLoadError",
    "PluginManifest",
    "PluginPermissionError",
    "discover_plugins",
    "load_plugin",
]
