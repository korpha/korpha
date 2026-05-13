"""TUI theme schema — terminal palettes for Textual.

Distinct from the dashboard's CSS-variable theme system because
terminals don't have arbitrary RGB. Textual uses semantic tokens
($primary, $accent, $surface, $boost, $warning, $error, $text,
$text-muted) which the renderer maps to ANSI colors.

A theme here is a flat dict[str, str] of token → hex (or named
ANSI). Loaded from:

  1. Built-in presets in ``BUILTIN_THEMES``.
  2. ``~/.korpha/tui-themes/<name>.yaml`` for user themes.

The active theme persists in ``~/.korpha/tui_state.json`` so it
sticks across launches.

Why hex per token + Textual: hex hits 256-color terminals
correctly + degrades to 16-color on basic terminals. Named ANSI
("blue", "yellow") is too narrow on dark backgrounds.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TuiTheme:
    """A complete TUI palette. Every field has a sensible default
    so partial themes (just override the accent) still produce a
    coherent look."""

    name: str
    label: str
    description: str = ""
    primary: str = "#5e9eff"
    accent: str = "#e7c14b"
    surface: str = "#0c0d10"
    boost: str = "#1c1d20"
    warning: str = "#f59e0b"
    error: str = "#ef4444"
    success: str = "#10b981"
    text: str = "#e6e8eb"
    text_muted: str = "#9aa0a6"

    def as_textual_variables(self) -> dict[str, str]:
        """Map to Textual's semantic token names (used in App.CSS)."""
        return {
            "primary": self.primary,
            "primary-background": self.surface,
            "primary-background-lighten-1": self.boost,
            "accent": self.accent,
            "surface": self.surface,
            "boost": self.boost,
            "warning": self.warning,
            "error": self.error,
            "success": self.success,
            "text": self.text,
            "text-muted": self.text_muted,
        }


BUILTIN_THEMES: dict[str, TuiTheme] = {
    "default": TuiTheme(
        name="default",
        label="Default (dark blue)",
        description="The shipping Korpha palette — neutral dark + warm accent",
    ),
    "midnight": TuiTheme(
        name="midnight",
        label="Midnight (deep, low-contrast)",
        description="Easier on the eyes for late-night work",
        primary="#3b82f6",
        accent="#a78bfa",
        surface="#020617",
        boost="#0f172a",
        text="#e2e8f0",
        text_muted="#64748b",
    ),
    "sage": TuiTheme(
        name="sage",
        label="Sage (greenish, calm)",
        description="Subtle green palette for long focused sessions",
        primary="#22c55e",
        accent="#84cc16",
        surface="#0f1611",
        boost="#1a241d",
        warning="#eab308",
        text="#e7f0e8",
        text_muted="#94a39a",
    ),
    "ember": TuiTheme(
        name="ember",
        label="Ember (warm orange)",
        description="High-contrast warm palette",
        primary="#f97316",
        accent="#fbbf24",
        surface="#1a0e0a",
        boost="#2d1810",
        text="#f5e6dc",
        text_muted="#b39080",
    ),
    "matrix": TuiTheme(
        name="matrix",
        label="Matrix (monochrome green)",
        description="Pure terminal nostalgia",
        primary="#22c55e",
        accent="#86efac",
        surface="#000000",
        boost="#0a1f0a",
        warning="#facc15",
        error="#f87171",
        text="#86efac",
        text_muted="#4ade80",
    ),
    "high-contrast": TuiTheme(
        name="high-contrast",
        label="High contrast (accessibility)",
        description="Max readable — for projector / shared screen / vision needs",
        primary="#ffffff",
        accent="#fbbf24",
        surface="#000000",
        boost="#1f2937",
        text="#ffffff",
        text_muted="#d1d5db",
    ),
}


def _user_theme_dir() -> Path:
    import os
    base = os.getenv("KORPHA_DATA_DIR")
    return (Path(base) if base else Path.home() / ".korpha") / "tui-themes"


def load_user_themes() -> dict[str, TuiTheme]:
    """Load every YAML under ``~/.korpha/tui-themes/``. Errors on
    individual files log + skip — one bad theme can't break the
    rest."""
    import yaml
    out: dict[str, TuiTheme] = {}
    root = _user_theme_dir()
    if not root.exists():
        return out
    for f in sorted(root.glob("*.yaml")):
        try:
            body = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if not isinstance(body, dict):
                continue
            name = str(body.get("name") or f.stem)
            theme = TuiTheme(
                name=name,
                label=str(body.get("label") or name.title()),
                description=str(body.get("description") or ""),
                **{
                    k: str(v) for k, v in body.items()
                    if k in {
                        "primary", "accent", "surface", "boost",
                        "warning", "error", "success",
                        "text", "text_muted",
                    } and isinstance(v, str)
                },
            )
            out[name] = theme
        except Exception as exc:
            logger.warning("failed to load TUI theme %s: %s", f, exc)
    return out


def all_themes() -> dict[str, TuiTheme]:
    """Built-ins + user themes, user wins on collision."""
    out = dict(BUILTIN_THEMES)
    out.update(load_user_themes())
    return out


def _state_path() -> Path:
    import os
    base = os.getenv("KORPHA_DATA_DIR")
    return (Path(base) if base else Path.home() / ".korpha") / "tui_state.json"


def get_active_theme_name() -> str:
    """Read the persisted active theme name. Defaults to ``"default"``."""
    path = _state_path()
    if not path.exists():
        return "default"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(body, dict):
            return str(body.get("theme") or "default")
    except (OSError, json.JSONDecodeError):
        pass
    return "default"


def set_active_theme_name(name: str) -> None:
    """Persist the selected theme name. Best-effort — failures are
    silent so a borked filesystem doesn't crash on /theme."""
    import contextlib
    path = _state_path()
    body: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                body = existing
        except (OSError, json.JSONDecodeError):
            pass
    body["theme"] = name
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")


__all__ = [
    "BUILTIN_THEMES",
    "TuiTheme",
    "all_themes",
    "get_active_theme_name",
    "load_user_themes",
    "set_active_theme_name",
]
