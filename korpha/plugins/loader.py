"""Plugin manifest parser + module-import loader.

Schema (``plugin.yaml``):

```yaml
name: niche-finder-pro              # required, unique
version: 1.0.0
description: Pro-grade niche scoring with proprietary data
author: jane_dev
entry_point: niche_finder_pro:register   # module:fn — fn(host) -> None
                                         # OR a path to a .py file
permissions:
  - skills            # may add skills
  - wakeup_handlers   # may register heartbeat handlers
  # - mcp_servers
  # - channel_adapters
```

Either ``module:fn`` (importable from sys.path / installed package) or
``./relative/path.py:fn`` (file inside the plugin directory) is accepted.
The plugin's directory is appended to ``sys.path`` during the import so
the entry-point module can pull in sibling modules without packaging.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from korpha.plugins.host import PluginHost

DEFAULT_PLUGINS_DIR = Path.home() / ".korpha" / "plugins"

VALID_PERMISSIONS = frozenset({
    "skills",
    "wakeup_handlers",
    "mcp_servers",
    "channel_adapters",
    "inference_providers",
})

ENTRY_POINT_GROUP = "korpha.plugins"
"""Python entry-point group for pip-installed plugins.

A plugin distributed as a wheel adds:

    [project.entry-points."korpha.plugins"]
    my-plugin = "my_plugin:plugin_manifest"

where ``plugin_manifest`` is a callable returning a ``PluginManifest``
(or the manifest itself). The entry-point loader takes care of
discovery without requiring the user to drop files into
``~/.korpha/plugins/``."""


class PluginLoadError(ValueError):
    """Plugin manifest is malformed, points to a missing entry point, or
    raised an unhandled exception during register()."""


@dataclass
class PluginManifest:
    name: str
    version: str
    description: str
    author: str
    entry_point: str
    permissions: frozenset[str]
    source_path: Path
    """Directory containing the manifest."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Anything else from the YAML — kept for forward compatibility."""


def plugins_dir() -> Path:
    import os

    override = os.getenv("KORPHA_PLUGINS_DIR")
    return Path(override).expanduser() if override else DEFAULT_PLUGINS_DIR


def discover_plugins(root: Path | None = None) -> list[PluginManifest]:
    """Find every immediate subdirectory of ``root`` containing a
    ``plugin.yaml``. Errors are collected and re-raised together so one
    bad manifest doesn't hide the others."""
    base = root or plugins_dir()
    if not base.exists():
        return []
    manifests: list[PluginManifest] = []
    errors: list[str] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "plugin.yaml"
        if not manifest_path.exists():
            continue
        try:
            manifests.append(parse_manifest(manifest_path))
        except PluginLoadError as exc:
            errors.append(f"  {entry.name}: {exc}")
    if errors:
        raise PluginLoadError("Plugin manifest errors:\n" + "\n".join(errors))
    return manifests


def parse_manifest(path: Path) -> PluginManifest:
    """Parse a plugin.yaml file."""
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise PluginLoadError(f"{path}: top level must be a mapping")

    name = _require_str(raw, "name", path)
    entry_point = _require_str(raw, "entry_point", path)

    perms_raw = raw.get("permissions") or []
    if not isinstance(perms_raw, list):
        raise PluginLoadError(f"{path}: permissions must be a list")
    perms = frozenset(str(p) for p in perms_raw)
    unknown = perms - VALID_PERMISSIONS
    if unknown:
        valid = ", ".join(sorted(VALID_PERMISSIONS))
        raise PluginLoadError(
            f"{path}: unknown permissions {sorted(unknown)}. Valid: {valid}"
        )

    return PluginManifest(
        name=name,
        version=str(raw.get("version", "0.0.0")),
        description=str(raw.get("description", "")),
        author=str(raw.get("author", "")),
        entry_point=entry_point,
        permissions=perms,
        source_path=path.parent,
        extras={
            k: v
            for k, v in raw.items()
            if k
            not in {
                "name",
                "version",
                "description",
                "author",
                "entry_point",
                "permissions",
            }
        },
    )


def load_plugin(
    manifest: PluginManifest,
    host: PluginHost,
) -> None:
    """Resolve the entry point, import it, call ``register(host)``.

    Raises ``PluginLoadError`` for any import / resolution failure. The
    host enforces capability gating itself, so we don't filter calls
    here — the plugin sees a fully-formed PluginHost and any out-of-
    permission operations raise PluginPermissionError at call time."""
    try:
        fn = _resolve_entry_point(manifest)
    except (ImportError, AttributeError, FileNotFoundError) as exc:
        raise PluginLoadError(
            f"plugin {manifest.name!r}: cannot import "
            f"entry_point {manifest.entry_point!r}: {exc}"
        ) from exc

    if not callable(fn):
        raise PluginLoadError(
            f"plugin {manifest.name!r}: entry_point "
            f"{manifest.entry_point!r} resolved to non-callable {type(fn).__name__}"
        )

    try:
        fn(host)
    except Exception as exc:
        raise PluginLoadError(
            f"plugin {manifest.name!r}: register() raised: {exc}"
        ) from exc


def load_all(
    manifests: Iterable[PluginManifest],
    *,
    host_factory: Callable[[PluginManifest], PluginHost],
) -> list[PluginHost]:
    """Convenience: build a host per manifest via ``host_factory`` and
    invoke each plugin's register(). Returns the list of hosts so the
    caller can inspect what was contributed."""
    hosts: list[PluginHost] = []
    for m in manifests:
        host = host_factory(m)
        load_plugin(m, host)
        hosts.append(host)
    return hosts


def _resolve_entry_point(manifest: PluginManifest) -> Any:
    spec = manifest.entry_point
    if ":" not in spec:
        raise PluginLoadError(
            f"plugin {manifest.name!r}: entry_point must be 'module:fn' "
            f"or 'path.py:fn', got {spec!r}"
        )
    target, attr = spec.rsplit(":", 1)

    # File path form: "./mymod.py:register" or "mymod.py:register"
    if target.endswith(".py") or "/" in target or target.startswith("."):
        file_path = (manifest.source_path / target).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        module_name = f"_korpha_plugin_{manifest.name.replace('-', '_')}"
        spec_obj = importlib.util.spec_from_file_location(module_name, file_path)
        if spec_obj is None or spec_obj.loader is None:
            raise PluginLoadError(
                f"plugin {manifest.name!r}: importlib could not load {file_path}"
            )
        module = importlib.util.module_from_spec(spec_obj)
        sys.modules[module_name] = module
        spec_obj.loader.exec_module(module)
    else:
        # Module path form: "mypkg.mymod:register"
        # Add the plugin's source dir to sys.path temporarily so
        # bundled-but-unpackaged modules resolve.
        plugin_dir = str(manifest.source_path)
        added = False
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
            added = True
        try:
            module = importlib.import_module(target)
        finally:
            if added:
                with _suppress(ValueError):
                    sys.path.remove(plugin_dir)

    return getattr(module, attr)


def _require_str(raw: dict[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginLoadError(f"{path}: missing required string {key!r}")
    return value


class _suppress:
    """Tiny stand-in for contextlib.suppress kept inline to avoid the import."""

    def __init__(self, *exc: type[BaseException]) -> None:
        self._exc = exc

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> bool:
        return exc_type is not None and issubclass(exc_type, self._exc)


def discover_entry_point_plugins() -> list[PluginManifest]:
    """Find plugins installed via pip + declared in the
    ``korpha.plugins`` entry-point group.

    Each entry point either IS a ``PluginManifest`` or is a callable
    that returns one. The callable form lets the entry-point function
    inspect the runtime before declaring its manifest (rare; useful for
    plugins that adjust permissions per-platform).

    Failures don't stop the iteration — bad entry points are logged
    and skipped. The point is graceful degradation when one stale
    install can't import; the others should still load.
    """
    import importlib.metadata
    import logging

    log = logging.getLogger(__name__)
    out: list[PluginManifest] = []
    try:
        eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:
        log.warning("entry_points lookup failed: %s", exc)
        return out

    for ep in eps:
        try:
            obj = ep.load()
            manifest = obj() if callable(obj) else obj
            if not isinstance(manifest, PluginManifest):
                log.warning(
                    "entry-point %s did not yield a PluginManifest "
                    "(got %s)", ep.name, type(manifest).__name__,
                )
                continue
            out.append(manifest)
        except Exception as exc:
            log.warning("failed to load entry-point plugin %s: %s", ep.name, exc)
    return out


def discover_all_plugins(
    *,
    root: Path | None = None,
    include_entry_points: bool = True,
) -> list[PluginManifest]:
    """Combined discovery: directory plugins (~/.korpha/plugins/)
    plus pip-installed entry-point plugins.

    Directory + entry-point name collisions: directory wins (last
    register). This lets a user shadow an installed plugin with a
    local fork by dropping it in ``~/.korpha/plugins/<name>/``.
    """
    seen: dict[str, PluginManifest] = {}
    if include_entry_points:
        for m in discover_entry_point_plugins():
            seen[m.name] = m
    for m in discover_plugins(root):
        seen[m.name] = m  # local override
    return list(seen.values())


def filter_enabled(
    manifests: list[PluginManifest],
    *,
    enabled: set[str] | None = None,
    disabled: set[str] | None = None,
) -> list[PluginManifest]:
    """Apply opt-in policy to discovered manifests.

    Default policy (Hermes-derived): plugins are OPT-IN. ``enabled``
    is the allow-list — only plugins whose name appears get loaded.
    ``disabled`` always wins on conflict.

    Resolution priority:
      1. ``disabled`` takes precedence: a plugin in disabled is dropped
         even if also in enabled.
      2. If ``enabled`` is None: load nothing (Mike opted into nothing).
      3. If ``enabled`` is a set: load only plugins whose name is in it.
      4. Special token ``"*"`` in ``enabled`` means "load everything"
         — the YOLO escape hatch for users who know what they're doing.

    Callers typically build ``enabled`` / ``disabled`` from the env
    vars ``KORPHA_PLUGINS_ENABLED`` (comma-separated) and
    ``KORPHA_PLUGINS_DISABLED``, or from a config file.
    """
    enabled = enabled if enabled is not None else set()
    disabled = disabled or set()
    out: list[PluginManifest] = []
    for m in manifests:
        if m.name in disabled:
            continue
        if "*" in enabled or m.name in enabled:
            out.append(m)
    return out


def enabled_set_from_env() -> set[str]:
    """Parse ``KORPHA_PLUGINS_ENABLED`` into a set. Empty / unset =
    empty set (no plugins loaded by default — opt-in is the rule).

    Comma + whitespace are valid separators. Token ``"*"`` enables all.
    """
    import os

    raw = os.getenv("KORPHA_PLUGINS_ENABLED", "").strip()
    if not raw:
        return set()
    return {t.strip() for t in raw.replace(",", " ").split() if t.strip()}


def disabled_set_from_env() -> set[str]:
    import os

    raw = os.getenv("KORPHA_PLUGINS_DISABLED", "").strip()
    if not raw:
        return set()
    return {t.strip() for t in raw.replace(",", " ").split() if t.strip()}


__all__ = [
    "DEFAULT_PLUGINS_DIR",
    "ENTRY_POINT_GROUP",
    "VALID_PERMISSIONS",
    "PluginLoadError",
    "PluginManifest",
    "disabled_set_from_env",
    "discover_all_plugins",
    "discover_entry_point_plugins",
    "discover_plugins",
    "enabled_set_from_env",
    "filter_enabled",
    "load_all",
    "load_plugin",
    "parse_manifest",
    "plugins_dir",
]
