"""Built-in dashboard themes.

Four ship out of the box:

  - ``default``  — the existing Korpha dark with blue accent
  - ``midnight`` — deep blue-violet, monospaced display font
  - ``sage``     — RankMyAnswer-branded green (Cofounder Protocol partner)
  - ``ember``    — warm crimson + bronze, for users who want gravitas

Built-ins live as Python data so they ship with the package and never
need a YAML file. User themes drop at ``~/.korpha/dashboard-themes/``
and override built-ins with the same name.
"""
from __future__ import annotations

from korpha.themes.types import (
    DashboardTheme,
    ThemeColorOverrides,
    ThemeLayer,
    ThemeLayout,
    ThemePalette,
    ThemeTypography,
)

# ---------------------------------------------------------------------------
# default — current Korpha dark, blue accent
# ---------------------------------------------------------------------------

DEFAULT_THEME = DashboardTheme(
    name="default",
    label="Korpha Dark",
    description="The original dark mode — deep slate with blue accents.",
    palette=ThemePalette(
        background=ThemeLayer(hex="#0c0d10"),
        midground=ThemeLayer(hex="#e6e8eb"),
        foreground=ThemeLayer(hex="#ffffff", alpha=0.0),
        warm_glow="rgba(94, 158, 255, 0.06)",
        noise_opacity=0.0,
    ),
    typography=ThemeTypography(
        base_size="14px",
        line_height="1.5",
        letter_spacing="0",
    ),
    layout=ThemeLayout(radius="0.5rem", density="comfortable"),
    color_overrides=ThemeColorOverrides(
        primary="#5e9eff",
        accent="#5e9eff",
        success="#6dcf80",
        warning="#e9c46a",
        destructive="#e76f74",
        border="#232730",
    ),
)


# ---------------------------------------------------------------------------
# midnight — deep blue-violet
# ---------------------------------------------------------------------------

MIDNIGHT_THEME = DashboardTheme(
    name="midnight",
    label="Midnight",
    description="Deep indigo + violet — feels like coding at 1am with the lights off.",
    palette=ThemePalette(
        background=ThemeLayer(hex="#0a0a1a"),
        midground=ThemeLayer(hex="#dcd9f4"),
        foreground=ThemeLayer(hex="#ffffff", alpha=0.0),
        warm_glow="rgba(139, 92, 246, 0.10)",
        noise_opacity=0.0,
    ),
    typography=ThemeTypography(
        font_sans=(
            "'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif"
        ),
        font_mono=(
            "'JetBrains Mono', ui-monospace, 'SF Mono', monospace"
        ),
        font_url=(
            "https://fonts.googleapis.com/css2?"
            "family=Inter:wght@400;500;600&"
            "family=JetBrains+Mono:wght@400;500&display=swap"
        ),
        base_size="14px",
        line_height="1.55",
        letter_spacing="-0.005em",
    ),
    layout=ThemeLayout(radius="0.625rem", density="comfortable"),
    color_overrides=ThemeColorOverrides(
        primary="#a78bfa",
        accent="#a78bfa",
        success="#6dcf80",
        warning="#fbbf24",
        destructive="#f87171",
        border="#1f1f3a",
    ),
)


# ---------------------------------------------------------------------------
# sage — RankMyAnswer-branded green (first Cofounder Protocol partner)
# ---------------------------------------------------------------------------

SAGE_THEME = DashboardTheme(
    name="sage",
    label="Sage (RankMyAnswer)",
    description=(
        "Soft sage + cream — the RankMyAnswer brand palette. "
        "Calm, grown-up, less screen-burny."
    ),
    palette=ThemePalette(
        background=ThemeLayer(hex="#0f1611"),
        midground=ThemeLayer(hex="#e8efe8"),
        foreground=ThemeLayer(hex="#ffffff", alpha=0.0),
        warm_glow="rgba(143, 188, 143, 0.10)",
        noise_opacity=0.0,
    ),
    typography=ThemeTypography(
        base_size="14px",
        line_height="1.55",
        letter_spacing="0",
    ),
    layout=ThemeLayout(radius="0.5rem", density="comfortable"),
    color_overrides=ThemeColorOverrides(
        primary="#1f7a4d",
        accent="#3d9970",
        success="#5ba66f",
        warning="#d4a45c",
        destructive="#c97171",
        border="#1d2620",
    ),
)


# ---------------------------------------------------------------------------
# ember — warm crimson + bronze (for users who want gravitas)
# ---------------------------------------------------------------------------

EMBER_THEME = DashboardTheme(
    name="ember",
    label="Ember",
    description=(
        "Warm crimson + bronze. For when shipping feels less like coding "
        "and more like forge work."
    ),
    palette=ThemePalette(
        background=ThemeLayer(hex="#150a0a"),
        midground=ThemeLayer(hex="#f4e8d8"),
        foreground=ThemeLayer(hex="#ffffff", alpha=0.0),
        warm_glow="rgba(205, 127, 50, 0.14)",
        noise_opacity=0.0,
    ),
    typography=ThemeTypography(
        font_sans=(
            "'Spectral', Georgia, 'Times New Roman', serif"
        ),
        font_mono=(
            "'IBM Plex Mono', ui-monospace, 'SF Mono', monospace"
        ),
        font_url=(
            "https://fonts.googleapis.com/css2?"
            "family=Spectral:wght@400;500;600&"
            "family=IBM+Plex+Mono:wght@400;500&display=swap"
        ),
        base_size="15px",
        line_height="1.6",
        letter_spacing="0.005em",
    ),
    layout=ThemeLayout(radius="0.25rem", density="comfortable"),
    color_overrides=ThemeColorOverrides(
        primary="#cd7f32",
        accent="#e9b970",
        success="#7caf6f",
        warning="#e9c46a",
        destructive="#c95a4a",
        border="#2a1818",
    ),
)


BUILTIN_THEMES: dict[str, DashboardTheme] = {
    DEFAULT_THEME.name: DEFAULT_THEME,
    MIDNIGHT_THEME.name: MIDNIGHT_THEME,
    SAGE_THEME.name: SAGE_THEME,
    EMBER_THEME.name: EMBER_THEME,
}


__all__ = [
    "BUILTIN_THEMES",
    "DEFAULT_THEME",
    "EMBER_THEME",
    "MIDNIGHT_THEME",
    "SAGE_THEME",
]
