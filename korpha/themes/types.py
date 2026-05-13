"""Dashboard theme schema — mirrors Hermes-agent v0.12.

Three orthogonal layers:

1. ``palette`` — 3-layer triplet (background / midground / foreground)
   + warm-glow + noise. Every shadcn-compat token derives from this.
2. ``typography`` — fonts + base size + spacing.
3. ``layout`` — radius + density.

Plus optional ``assets`` (bg/hero/logo/crest), ``customCSS`` (raw
injected style block), ``componentStyles`` (per-component CSS-var
buckets), ``colorOverrides`` (pin shadcn tokens to exact hex).

The validator is strict on required fields (palette + typography +
layout) so authors get clear errors at load time, not on display.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class ThemeValidationError(ValueError):
    """A theme YAML / dict failed schema validation."""


@dataclass(frozen=True)
class ThemeLayer:
    """One color layer: hex base + alpha multiplier (0-1)."""

    hex: str
    alpha: float = 1.0


@dataclass(frozen=True)
class ThemePalette:
    """3-layer triplet that drives every derived token."""

    background: ThemeLayer
    """Deepest canvas color (typically near-black for dark themes)."""

    midground: ThemeLayer
    """Primary text + accent color. Most chrome reads from this."""

    foreground: ThemeLayer
    """Top-layer highlight. Often white at low alpha — invisible by
    default but drives ring / focus accents."""

    warm_glow: str = "rgba(255, 199, 55, 0.0)"
    """Warm vignette as an rgba() string. Set alpha 0 to disable."""

    noise_opacity: float = 0.0
    """Scalar (0-1.2) for the subtle grain overlay."""


@dataclass(frozen=True)
class ThemeTypography:
    font_sans: str = "system-ui, -apple-system, 'Segoe UI', sans-serif"
    font_mono: str = "ui-monospace, 'SF Mono', 'Menlo', monospace"
    font_display: str | None = None
    """Optional display/heading stack. Falls back to font_sans."""

    font_url: str | None = None
    """Optional Google/Bunny/self-hosted .woff2 URL. Injected as a
    <link rel="stylesheet"> in <head> — never injected twice."""

    base_size: str = "14px"
    line_height: str = "1.5"
    letter_spacing: str = "0"


ThemeDensity = str  # "compact" | "comfortable" | "spacious"
_VALID_DENSITIES = ("compact", "comfortable", "spacious")

ThemeLayoutVariant = str  # "standard" | "cockpit" | "tiled"
_VALID_LAYOUT_VARIANTS = ("standard", "cockpit", "tiled")


@dataclass(frozen=True)
class ThemeLayout:
    radius: str = "0.5rem"
    density: ThemeDensity = "comfortable"


@dataclass(frozen=True)
class ThemeAssets:
    """Named asset URLs exposed as ``--theme-asset-<name>`` CSS vars."""

    bg: str | None = None
    hero: str | None = None
    logo: str | None = None
    crest: str | None = None
    sidebar: str | None = None
    header: str | None = None
    custom: dict[str, str] = field(default_factory=dict)
    """Arbitrary named assets, keyed by ``[a-zA-Z0-9_-]`` only.
    Emitted as ``--theme-asset-custom-<key>``."""


@dataclass(frozen=True)
class ThemeComponentStyles:
    """Per-component CSS-var buckets. Each entry's keys become
    ``--component-<bucket>-<kebab-property>`` vars that components
    read. Values are plain CSS — no parsing, so authors can use
    clip-path, border-image, gradients, anything CSS accepts."""

    card: dict[str, str] = field(default_factory=dict)
    header: dict[str, str] = field(default_factory=dict)
    footer: dict[str, str] = field(default_factory=dict)
    sidebar: dict[str, str] = field(default_factory=dict)
    tab: dict[str, str] = field(default_factory=dict)
    progress: dict[str, str] = field(default_factory=dict)
    badge: dict[str, str] = field(default_factory=dict)
    backdrop: dict[str, str] = field(default_factory=dict)
    page: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ThemeColorOverrides:
    """Optional hex pins for specific shadcn-compat tokens."""

    card: str | None = None
    card_foreground: str | None = None
    popover: str | None = None
    popover_foreground: str | None = None
    primary: str | None = None
    primary_foreground: str | None = None
    secondary: str | None = None
    secondary_foreground: str | None = None
    muted: str | None = None
    muted_foreground: str | None = None
    accent: str | None = None
    accent_foreground: str | None = None
    destructive: str | None = None
    destructive_foreground: str | None = None
    success: str | None = None
    warning: str | None = None
    border: str | None = None
    input: str | None = None
    ring: str | None = None


@dataclass(frozen=True)
class DashboardTheme:
    """One full theme definition — what the picker switches between."""

    name: str
    """Snake-case identifier. Stored in config.dashboard.theme."""

    label: str
    """Human-readable name shown in the picker."""

    description: str
    """One-line description shown under the label in the picker."""

    palette: ThemePalette
    typography: ThemeTypography = field(default_factory=ThemeTypography)
    layout: ThemeLayout = field(default_factory=ThemeLayout)

    layout_variant: ThemeLayoutVariant = "standard"
    """``standard`` = default; ``cockpit`` reserves a sidebar rail
    for plugin slots; ``tiled`` relaxes max-width."""

    assets: ThemeAssets = field(default_factory=ThemeAssets)
    custom_css: str = ""
    """Raw CSS injected as a scoped <style> on theme apply."""

    component_styles: ThemeComponentStyles = field(
        default_factory=ThemeComponentStyles
    )
    color_overrides: ThemeColorOverrides = field(
        default_factory=ThemeColorOverrides
    )


@dataclass(frozen=True)
class ThemeListEntry:
    """Wire-format entry for ``GET /api/dashboard/themes``.

    Built-ins ship name/label/description only — the dashboard already
    has their full definitions. User themes ship their full normalized
    ``definition`` so the picker can render real palette swatches
    without a second round-trip (the May-4 Hermes fix).
    """

    name: str
    label: str
    description: str
    is_builtin: bool
    definition: DashboardTheme | None = None


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_CUSTOM_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def parse_theme(raw: Any, *, source: str = "<dict>") -> DashboardTheme:
    """Validate a raw mapping (loaded from YAML / JSON) into a theme.

    Raises ``ThemeValidationError`` on the first failure with a
    path-like message so authors can debug their YAML quickly.
    """
    if not isinstance(raw, dict):
        raise ThemeValidationError(
            f"{source}: theme must be a mapping, got {type(raw).__name__}"
        )

    name = _required_str(raw, "name", source)
    if not _NAME_RE.match(name):
        raise ThemeValidationError(
            f"{source}: 'name' must be lowercase a-z0-9_- (snake-case-ish); got {name!r}"
        )
    label = _required_str(raw, "label", source)
    description = _required_str(raw, "description", source)
    palette = _parse_palette(raw.get("palette"), source=source)

    typography = _parse_typography(raw.get("typography"), source=source)
    layout = _parse_layout(raw.get("layout"), source=source)

    layout_variant = raw.get("layout_variant", raw.get("layoutVariant", "standard"))
    if layout_variant not in _VALID_LAYOUT_VARIANTS:
        raise ThemeValidationError(
            f"{source}: layout_variant must be one of {_VALID_LAYOUT_VARIANTS}; "
            f"got {layout_variant!r}"
        )

    assets = _parse_assets(raw.get("assets"), source=source)
    custom_css = raw.get("custom_css") or raw.get("customCSS") or ""
    if not isinstance(custom_css, str):
        raise ThemeValidationError(
            f"{source}: custom_css must be a string"
        )

    component_styles = _parse_component_styles(
        raw.get("component_styles") or raw.get("componentStyles"),
        source=source,
    )
    color_overrides = _parse_color_overrides(
        raw.get("color_overrides") or raw.get("colorOverrides"),
        source=source,
    )

    return DashboardTheme(
        name=name,
        label=label,
        description=description,
        palette=palette,
        typography=typography,
        layout=layout,
        layout_variant=layout_variant,
        assets=assets,
        custom_css=custom_css,
        component_styles=component_styles,
        color_overrides=color_overrides,
    )


def _required_str(raw: dict[str, Any], key: str, source: str) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ThemeValidationError(
            f"{source}: missing or empty required string field {key!r}"
        )
    return val.strip()


def _parse_layer(raw: Any, key: str, source: str) -> ThemeLayer:
    """Accept either a hex string OR a {hex, alpha} mapping."""
    if isinstance(raw, str):
        if not _HEX_RE.match(raw):
            raise ThemeValidationError(
                f"{source}: palette.{key} must be a #RRGGBB hex; got {raw!r}"
            )
        return ThemeLayer(hex=raw, alpha=1.0)
    if isinstance(raw, dict):
        hex_val = raw.get("hex")
        if not isinstance(hex_val, str) or not _HEX_RE.match(hex_val):
            raise ThemeValidationError(
                f"{source}: palette.{key}.hex must be a #RRGGBB hex"
            )
        alpha = raw.get("alpha", 1.0)
        try:
            alpha_float = float(alpha)
        except (TypeError, ValueError) as exc:
            raise ThemeValidationError(
                f"{source}: palette.{key}.alpha must be a number (0-1)"
            ) from exc
        if not 0.0 <= alpha_float <= 1.0:
            raise ThemeValidationError(
                f"{source}: palette.{key}.alpha must be between 0 and 1; got {alpha_float}"
            )
        return ThemeLayer(hex=hex_val, alpha=alpha_float)
    raise ThemeValidationError(
        f"{source}: palette.{key} must be a hex string or {{hex, alpha}} mapping"
    )


def _parse_palette(raw: Any, *, source: str) -> ThemePalette:
    if not isinstance(raw, dict):
        raise ThemeValidationError(
            f"{source}: 'palette' is required and must be a mapping"
        )
    bg = _parse_layer(raw.get("background"), "background", source)
    mg = _parse_layer(raw.get("midground"), "midground", source)
    fg = _parse_layer(raw.get("foreground"), "foreground", source)
    warm_glow = raw.get("warm_glow", raw.get("warmGlow", "rgba(255, 199, 55, 0.0)"))
    if not isinstance(warm_glow, str):
        raise ThemeValidationError(
            f"{source}: palette.warm_glow must be a string (rgba/hex/etc.)"
        )
    noise_opacity_raw = raw.get("noise_opacity", raw.get("noiseOpacity", 0.0))
    if noise_opacity_raw is None:
        noise_float = 0.0
    else:
        try:
            noise_float = float(noise_opacity_raw)
        except (TypeError, ValueError) as exc:
            raise ThemeValidationError(
                f"{source}: palette.noise_opacity must be a number"
            ) from exc
    return ThemePalette(
        background=bg,
        midground=mg,
        foreground=fg,
        warm_glow=warm_glow,
        noise_opacity=noise_float,
    )


def _parse_typography(raw: Any, *, source: str) -> ThemeTypography:
    if raw is None:
        return ThemeTypography()
    if not isinstance(raw, dict):
        raise ThemeValidationError(
            f"{source}: 'typography' must be a mapping if present"
        )
    return ThemeTypography(
        font_sans=str(
            raw.get("font_sans", raw.get("fontSans"))
            or ThemeTypography.__dataclass_fields__["font_sans"].default
        ),
        font_mono=str(
            raw.get("font_mono", raw.get("fontMono"))
            or ThemeTypography.__dataclass_fields__["font_mono"].default
        ),
        font_display=raw.get("font_display") or raw.get("fontDisplay"),
        font_url=raw.get("font_url") or raw.get("fontUrl"),
        base_size=str(
            raw.get("base_size", raw.get("baseSize"))
            or ThemeTypography.__dataclass_fields__["base_size"].default
        ),
        line_height=str(
            raw.get("line_height", raw.get("lineHeight"))
            or ThemeTypography.__dataclass_fields__["line_height"].default
        ),
        letter_spacing=str(
            raw.get("letter_spacing", raw.get("letterSpacing"))
            or ThemeTypography.__dataclass_fields__["letter_spacing"].default
        ),
    )


def _parse_layout(raw: Any, *, source: str) -> ThemeLayout:
    if raw is None:
        return ThemeLayout()
    if not isinstance(raw, dict):
        raise ThemeValidationError(
            f"{source}: 'layout' must be a mapping if present"
        )
    radius = str(raw.get("radius") or ThemeLayout.__dataclass_fields__["radius"].default)
    density = raw.get("density") or "comfortable"
    if density not in _VALID_DENSITIES:
        raise ThemeValidationError(
            f"{source}: layout.density must be one of {_VALID_DENSITIES}; got {density!r}"
        )
    return ThemeLayout(radius=radius, density=density)


def _parse_assets(raw: Any, *, source: str) -> ThemeAssets:
    if raw is None:
        return ThemeAssets()
    if not isinstance(raw, dict):
        raise ThemeValidationError(
            f"{source}: 'assets' must be a mapping if present"
        )
    custom = raw.get("custom") or {}
    if not isinstance(custom, dict):
        raise ThemeValidationError(
            f"{source}: assets.custom must be a mapping of key→url"
        )
    for k in custom:
        if not _CUSTOM_KEY_RE.match(str(k)):
            raise ThemeValidationError(
                f"{source}: assets.custom.{k!r} key must be alphanumeric/_/- only"
            )
    return ThemeAssets(
        bg=raw.get("bg"),
        hero=raw.get("hero"),
        logo=raw.get("logo"),
        crest=raw.get("crest"),
        sidebar=raw.get("sidebar"),
        header=raw.get("header"),
        custom={str(k): str(v) for k, v in custom.items()},
    )


def _parse_component_styles(raw: Any, *, source: str) -> ThemeComponentStyles:
    if raw is None:
        return ThemeComponentStyles()
    if not isinstance(raw, dict):
        raise ThemeValidationError(
            f"{source}: 'component_styles' must be a mapping if present"
        )
    valid_buckets = {
        "card", "header", "footer", "sidebar", "tab",
        "progress", "badge", "backdrop", "page",
    }
    out: dict[str, dict[str, str]] = {b: {} for b in valid_buckets}
    for bucket, entries in raw.items():
        if bucket not in valid_buckets:
            raise ThemeValidationError(
                f"{source}: unknown component bucket {bucket!r}; "
                f"valid: {sorted(valid_buckets)}"
            )
        if not isinstance(entries, dict):
            raise ThemeValidationError(
                f"{source}: component_styles.{bucket} must be a mapping"
            )
        out[bucket] = {str(k): str(v) for k, v in entries.items()}
    return ThemeComponentStyles(**out)


def _parse_color_overrides(raw: Any, *, source: str) -> ThemeColorOverrides:
    if raw is None:
        return ThemeColorOverrides()
    if not isinstance(raw, dict):
        raise ThemeValidationError(
            f"{source}: 'color_overrides' must be a mapping if present"
        )
    # Accept both snake_case and camelCase keys; normalize internally.
    snake_to_camel = {
        "card_foreground": "cardForeground",
        "popover_foreground": "popoverForeground",
        "primary_foreground": "primaryForeground",
        "secondary_foreground": "secondaryForeground",
        "muted_foreground": "mutedForeground",
        "accent_foreground": "accentForeground",
        "destructive_foreground": "destructiveForeground",
    }
    valid = set(ThemeColorOverrides.__dataclass_fields__.keys())
    parsed: dict[str, str | None] = {}
    for key, value in raw.items():
        snake = key
        for sn, cm in snake_to_camel.items():
            if key == cm:
                snake = sn
                break
        if snake not in valid:
            raise ThemeValidationError(
                f"{source}: unknown color_overrides key {key!r}"
            )
        if value is None:
            continue
        if not isinstance(value, str) or not _HEX_RE.match(value):
            raise ThemeValidationError(
                f"{source}: color_overrides.{key} must be a #RRGGBB hex; got {value!r}"
            )
        parsed[snake] = value
    return ThemeColorOverrides(**parsed)


__all__ = [
    "DashboardTheme",
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
    "ThemeValidationError",
    "parse_theme",
]
