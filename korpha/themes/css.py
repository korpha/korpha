"""Render a DashboardTheme into the CSS-variable block injected into
``<head>`` on every page. The dashboard's existing ``shell.css`` reads
these vars; theme switching = swapping this block.

The mapping mirrors the existing ``--bg / --bg-elev / --text / --accent``
vars so the theme system layers cleanly on top of the current shell
without rewriting components.
"""
from __future__ import annotations

from korpha.themes.types import (
    DashboardTheme,
    ThemeColorOverrides,
    ThemePalette,
)


def render_theme_css_vars(theme: DashboardTheme) -> str:
    """Return the contents of a ``:root { ... }`` style block — caller
    wraps it in ``<style>`` tags. Includes all derived vars the
    existing dashboard reads PLUS the new theme-specific vars
    (``--theme-asset-*``, ``--component-*``, etc.)."""

    parts: list[str] = [":root {"]

    # ---- palette + derived shell vars (back-compat with shell.css) ----
    bg = theme.palette.background.hex
    mg = theme.palette.midground.hex

    # Color overrides win over derived defaults
    co = theme.color_overrides
    accent = co.accent or co.primary or "#5e9eff"
    primary = co.primary or accent
    success = co.success or "#6dcf80"
    warning = co.warning or "#e9c46a"
    destructive = co.destructive or "#e76f74"
    border = co.border or _shift(bg, 0x10)
    # Contrast ratios on dark themes need text_dim ≥ ~4.5:1 and
    # text_faint ≥ ~3:1 against bg. Prior 0.55 / 0.35 alpha mix put
    # text_faint at ~2.5:1 which fails WCAG AA on dark backgrounds —
    # body copy + blocker details ended up unreadable. Bumped so even
    # the lowest-priority hint text stays legible.
    text = mg
    text_dim = _alpha_mix(mg, bg, 0.75)
    text_faint = _alpha_mix(mg, bg, 0.55)

    # Layered backgrounds for elevation surfaces
    bg_elev = _shift(bg, 0x05)
    bg_hover = _shift(bg, 0x10)
    bg_active = _shift(bg, 0x17)

    parts.extend([
        f"  --bg: {bg};",
        f"  --bg-elev: {bg_elev};",
        f"  --bg-hover: {bg_hover};",
        f"  --bg-active: {bg_active};",
        f"  --border: {border};",
        f"  --border-strong: {_shift(border, 0x0c)};",
        f"  --text: {text};",
        f"  --text-dim: {text_dim};",
        f"  --text-faint: {text_faint};",
        f"  --accent: {accent};",
        f"  --accent-soft: {_with_alpha(accent, 0.18)};",
        f"  --primary: {primary};",
        f"  --green: {success};",
        f"  --green-soft: {_with_alpha(success, 0.18)};",
        f"  --yellow: {warning};",
        f"  --yellow-soft: {_with_alpha(warning, 0.18)};",
        f"  --red: {destructive};",
        f"  --red-soft: {_with_alpha(destructive, 0.18)};",
        f"  --warm-glow: {theme.palette.warm_glow};",
        f"  --noise-opacity: {theme.palette.noise_opacity};",
    ])

    # ---- typography vars ----
    parts.extend([
        f"  --font-sans: {theme.typography.font_sans};",
        f"  --font-mono: {theme.typography.font_mono};",
        f"  --font-display: {theme.typography.font_display or theme.typography.font_sans};",
        f"  --font-base-size: {theme.typography.base_size};",
        f"  --font-line-height: {theme.typography.line_height};",
        f"  --font-letter-spacing: {theme.typography.letter_spacing};",
    ])

    # ---- layout vars ----
    density_mul = {
        "compact": "0.85",
        "comfortable": "1.0",
        "spacious": "1.2",
    }.get(theme.layout.density, "1.0")
    parts.extend([
        f"  --radius: {theme.layout.radius};",
        f"  --radius-sm: calc({theme.layout.radius} * 0.6);",
        f"  --radius-md: {theme.layout.radius};",
        f"  --radius-lg: calc({theme.layout.radius} * 1.6);",
        f"  --spacing-mul: {density_mul};",
    ])

    # ---- assets ----
    if theme.assets.bg:
        parts.append(f"  --theme-asset-bg: {_url_value(theme.assets.bg)};")
    if theme.assets.hero:
        parts.append(f"  --theme-asset-hero: {_url_value(theme.assets.hero)};")
    if theme.assets.logo:
        parts.append(f"  --theme-asset-logo: {_url_value(theme.assets.logo)};")
    if theme.assets.crest:
        parts.append(f"  --theme-asset-crest: {_url_value(theme.assets.crest)};")
    if theme.assets.sidebar:
        parts.append(f"  --theme-asset-sidebar: {_url_value(theme.assets.sidebar)};")
    if theme.assets.header:
        parts.append(f"  --theme-asset-header: {_url_value(theme.assets.header)};")
    for key, value in theme.assets.custom.items():
        parts.append(f"  --theme-asset-custom-{key}: {_url_value(value)};")

    # ---- per-component buckets ----
    for bucket_name in (
        "card", "header", "footer", "sidebar", "tab",
        "progress", "badge", "backdrop", "page",
    ):
        bucket = getattr(theme.component_styles, bucket_name)
        for prop, value in bucket.items():
            parts.append(
                f"  --component-{bucket_name}-{_kebab(prop)}: {value};"
            )

    parts.append("}")

    out = "\n".join(parts)
    if theme.custom_css:
        # Custom CSS is appended raw — author owns the scope. We
        # defensively wrap in a comment so it's identifiable in
        # devtools when debugging.
        out += (
            f"\n\n/* theme:{theme.name} customCSS */\n"
            f"{theme.custom_css}\n"
            f"/* /theme:{theme.name} customCSS */"
        )
    return out


# ---------------------------------------------------------------------------
# Color helpers — small enough to ship inline rather than pull a dep
# ---------------------------------------------------------------------------


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _shift(hex_str: str, delta: int) -> str:
    """Lighten/darken a hex color by ``delta`` per channel (signed)."""
    r, g, b = _hex_to_rgb(hex_str)
    # Decide direction: if it's a dark color, lighten; light color,
    # darken. Simple luma check (fast + good enough for shells).
    luma = (r + g + b) / 3
    sign = 1 if luma < 128 else -1
    return _rgb_to_hex(r + sign * delta, g + sign * delta, b + sign * delta)


def _with_alpha(hex_str: str, alpha: float) -> str:
    """Return ``rgba(r, g, b, a)`` for use as soft-fill backgrounds."""
    r, g, b = _hex_to_rgb(hex_str)
    return f"rgba({r}, {g}, {b}, {alpha:.3f})"


def _alpha_mix(fg_hex: str, bg_hex: str, alpha: float) -> str:
    """Pre-mix ``fg`` over ``bg`` at ``alpha`` (returns hex). Used for
    derived ``--text-dim`` style vars where we want a static hex
    instead of a computed rgba (better browser cache + simpler
    inheritance)."""
    fr, fg, fb = _hex_to_rgb(fg_hex)
    br, bgg, bb = _hex_to_rgb(bg_hex)
    r = int(fr * alpha + br * (1 - alpha))
    g = int(fg * alpha + bgg * (1 - alpha))
    b = int(fb * alpha + bb * (1 - alpha))
    return _rgb_to_hex(r, g, b)


def _url_value(value: str) -> str:
    """Asset values can be a URL, a data URL, OR an already-wrapped
    CSS expression (``url(...)`` / ``linear-gradient(...)``). We pass
    pre-wrapped expressions through; bare URLs get wrapped."""
    stripped = value.strip()
    pre_wrapped = (
        stripped.startswith(("url(", "linear-gradient(", "radial-gradient(", "conic-gradient("))
    )
    if pre_wrapped:
        return stripped
    return f"url('{stripped}')"


def _kebab(snake_or_camel: str) -> str:
    """Convert a property name to kebab-case for CSS-var compatibility."""
    out: list[str] = []
    for i, ch in enumerate(snake_or_camel):
        if ch == "_":
            out.append("-")
        elif ch.isupper() and i > 0 and snake_or_camel[i - 1] != "_":
            out.append("-")
            out.append(ch.lower())
        else:
            out.append(ch.lower())
    return "".join(out)


def render_font_link(theme: DashboardTheme) -> str:
    """Return the ``<link rel="stylesheet">`` tag for the theme's
    optional ``font_url``, or an empty string."""
    if theme.typography.font_url:
        return (
            f'<link rel="stylesheet" '
            f'href="{theme.typography.font_url}" '
            f'crossorigin="anonymous">'
        )
    return ""


__all__ = [
    "render_font_link",
    "render_theme_css_vars",
]


# Quiet the unused-import warning when ThemePalette / ThemeColorOverrides
# aren't directly referenced in this module's runtime path.
_ = (ThemePalette, ThemeColorOverrides)
