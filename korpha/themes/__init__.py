"""Dashboard themes — palette/typography/layout/assets, all CSS-var-driven.

Mirrors the Hermes-agent v0.12 theme system (web/src/themes/types.ts)
including the May-4 fix for showing custom-theme palette swatches.
Adapted to Korpha's server-rendered (Jinja2 + HTMX) dashboard
instead of Hermes' React SPA — the schema is identical, the
delivery surface is different.

Why mirror exactly: the Cofounder Protocol roadmap (v2) lets partners
ship a theme alongside their manifest. Sharing schema with Hermes
means the same YAML works in both worlds with zero translation.

User themes drop at ``~/.korpha/dashboard-themes/<name>.yaml``.
Built-ins ship in this module's ``presets.py``. Active theme persists
under ``dashboard.theme`` in the user's config.
"""
from __future__ import annotations

from korpha.themes.css import render_theme_css_vars
from korpha.themes.loader import (
    DashboardThemesError,
    discover_user_themes,
    get_active_theme_name,
    list_themes,
    load_theme_by_name,
    set_active_theme_name,
)
from korpha.themes.presets import BUILTIN_THEMES, DEFAULT_THEME
from korpha.themes.types import (
    DashboardTheme,
    ThemeAssets,
    ThemeColorOverrides,
    ThemeComponentStyles,
    ThemeDensity,
    ThemeLayer,
    ThemeLayout,
    ThemeLayoutVariant,
    ThemeListEntry,
    ThemePalette,
    ThemeTypography,
    parse_theme,
)

__all__ = [
    "BUILTIN_THEMES",
    "DEFAULT_THEME",
    "DashboardTheme",
    "DashboardThemesError",
    "ThemeAssets",
    "ThemeColorOverrides",
    "ThemeComponentStyles",
    "ThemeDensity",
    "ThemeLayer",
    "ThemeLayout",
    "ThemeLayoutVariant",
    "ThemeListEntry",
    "ThemePalette",
    "ThemeTypography",
    "discover_user_themes",
    "get_active_theme_name",
    "list_themes",
    "load_theme_by_name",
    "parse_theme",
    "render_theme_css_vars",
    "set_active_theme_name",
]
