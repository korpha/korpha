"""User theme YAML discovery + active theme persistence.

User themes drop YAML files at ``~/.korpha/dashboard-themes/<name>.yaml``.
The loader scans that dir at every list call (cheap — small dirs) so
authors don't have to restart the dashboard after editing.

Active theme name persists to ``~/.korpha/config.yaml`` under
``dashboard.theme``. Override path with ``KORPHA_DATA_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from korpha.themes.presets import BUILTIN_THEMES, DEFAULT_THEME
from korpha.themes.types import (
    DashboardTheme,
    ThemeListEntry,
    ThemeValidationError,
    parse_theme,
)


class DashboardThemesError(RuntimeError):
    """Couldn't load / save / find a theme."""


def _data_dir() -> Path:
    """Where ~/.korpha lives. ``KORPHA_DATA_DIR`` overrides for tests."""
    override = os.getenv("KORPHA_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".korpha"


def _user_themes_dir() -> Path:
    return _data_dir() / "dashboard-themes"


def _config_path() -> Path:
    """Where the active-theme name persists. Same dir as the providers
    config, just under ``dashboard:`` instead of ``providers:``."""
    return _data_dir() / "config.yaml"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_user_themes() -> list[DashboardTheme]:
    """Walk ``~/.korpha/dashboard-themes/*.yaml``, parse each, return
    the valid ones. Malformed YAMLs are skipped silently — a single
    bad file shouldn't take down the whole picker. (Errors surface
    when the user tries to apply that specific theme.)"""
    themes_dir = _user_themes_dir()
    if not themes_dir.is_dir():
        return []
    out: list[DashboardTheme] = []
    for path in sorted(themes_dir.glob("*.yaml")):
        try:
            theme = _load_theme_file(path)
        except (ThemeValidationError, OSError):
            continue
        out.append(theme)
    return out


def _load_theme_file(path: Path) -> DashboardTheme:
    """Parse one YAML file into a DashboardTheme. The filename's stem
    overrides any ``name`` field if missing — so authors can drop a
    file named ``my-theme.yaml`` and not bother setting name explicitly."""
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict) and "name" not in raw:
        raw = {**raw, "name": path.stem}
    return parse_theme(raw, source=str(path))


def list_themes() -> list[ThemeListEntry]:
    """Return built-ins + user themes for the dashboard picker.

    Built-ins ship name/label/description only — the dashboard already
    has their full definition (``BUILTIN_THEMES`` / ``presets.py``).
    User themes ship the full normalized definition inline so the
    picker can render real palette swatches without a second
    round-trip (Hermes' May-4 fix).
    """
    seen: set[str] = set()
    entries: list[ThemeListEntry] = []
    for theme in BUILTIN_THEMES.values():
        seen.add(theme.name)
        entries.append(
            ThemeListEntry(
                name=theme.name,
                label=theme.label,
                description=theme.description,
                is_builtin=True,
                definition=None,
            )
        )
    for theme in discover_user_themes():
        if theme.name in seen:
            # User can't shadow a built-in. To customize a built-in,
            # author a new theme with a different name.
            continue
        entries.append(
            ThemeListEntry(
                name=theme.name,
                label=theme.label,
                description=theme.description,
                is_builtin=False,
                definition=theme,
            )
        )
        seen.add(theme.name)
    return entries


def load_theme_by_name(name: str) -> DashboardTheme:
    """Return the full DashboardTheme for ``name``, built-in or user.

    Raises ``DashboardThemesError`` if not found OR if the user YAML
    is malformed (unlike ``discover_user_themes`` which silently
    skips — here the caller is asking for a specific theme so it
    deserves a real error).
    """
    builtin = BUILTIN_THEMES.get(name)
    if builtin is not None:
        return builtin
    candidate = _user_themes_dir() / f"{name}.yaml"
    if not candidate.exists():
        raise DashboardThemesError(
            f"Theme {name!r} not found. Built-ins: "
            f"{sorted(BUILTIN_THEMES)}. Drop a YAML at "
            f"{candidate} to add a custom theme."
        )
    try:
        return _load_theme_file(candidate)
    except ThemeValidationError as exc:
        raise DashboardThemesError(
            f"Theme {name!r} ({candidate}) is malformed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Active-theme persistence
# ---------------------------------------------------------------------------


def get_active_theme_name() -> str:
    """Return the name stored in config.yaml, or ``"default"``."""
    cfg = _read_config()
    name = cfg.get("dashboard", {}).get("theme") if isinstance(cfg, dict) else None
    if isinstance(name, str) and name:
        return name
    return DEFAULT_THEME.name


def set_active_theme_name(name: str) -> None:
    """Persist the active theme name to config.yaml. Validates that
    the theme exists first — no point setting an unresolvable name."""
    load_theme_by_name(name)  # raises DashboardThemesError if missing
    cfg = _read_config() or {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("dashboard", {})["theme"] = name
    _write_config(cfg)


def _read_config() -> dict[str, Any]:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        import yaml

        body = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _write_config(cfg: dict[str, Any]) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


__all__ = [
    "DashboardThemesError",
    "discover_user_themes",
    "get_active_theme_name",
    "list_themes",
    "load_theme_by_name",
    "set_active_theme_name",
]
