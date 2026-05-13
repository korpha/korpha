# Dashboard Theme Protocol — author guide

**Audience**: anyone who wants to ship a custom Korpha dashboard
theme. No code changes — single YAML file.

**TL;DR**: drop a file at `~/.korpha/dashboard-themes/<name>.yaml`,
hard-refresh the dashboard, pick your theme from the topbar palette
icon. That's it.

---

## Quick start (60 seconds)

```bash
mkdir -p ~/.korpha/dashboard-themes/
cat > ~/.korpha/dashboard-themes/ocean.yaml << 'EOF'
name: ocean
label: "Ocean"
description: "Cool deep-water blues and a soft seafoam glow."

palette:
  background: "#0a1929"
  midground: "#cfe7ff"
  foreground:
    hex: "#ffffff"
    alpha: 0
  warm_glow: "rgba(64, 169, 209, 0.10)"

color_overrides:
  primary: "#40a9d1"
  accent: "#5fbcdf"
  success: "#5ba66f"
  destructive: "#c97171"
  border: "#172a3f"
EOF
```

Refresh the dashboard. Click the topbar palette icon. Pick "Ocean."

The picker shows a real palette swatch derived from your `palette.background`,
`palette.midground`, and `palette.warm_glow`.

---

## Schema (v1)

Three required fields and a palette. Everything else is optional;
sensible defaults fill in for what you skip.

### Required

```yaml
name: my_theme              # snake-case-ish: lowercase a-z 0-9 _ -
label: "My Theme"            # human-readable, shown in the picker
description: "One line."     # tagline shown under the label
palette:                     # the 3-layer triplet — see below
  background: "#0c0d10"      # deepest canvas color (hex)
  midground:  "#e6e8eb"      # primary text + accent (hex)
  foreground:                # top highlight (often white at low alpha)
    hex: "#ffffff"
    alpha: 0                 # 0-1
```

You can write `background` as either a bare hex string OR a
`{hex, alpha}` mapping. Same for any layer.

### Optional palette extras

```yaml
palette:
  warm_glow: "rgba(255, 199, 55, 0.06)"   # vignette color (rgba/hex/css color)
  noise_opacity: 0                          # 0-1.2 noise overlay multiplier
```

### Typography

```yaml
typography:
  font_sans: "'Inter', system-ui, sans-serif"
  font_mono: "'JetBrains Mono', ui-monospace, monospace"
  font_display: "'Orbitron', sans-serif"     # optional, falls back to font_sans
  font_url: "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap"
  base_size: "14px"
  line_height: "1.55"
  letter_spacing: "0"
```

`font_url` is injected as `<link rel="stylesheet">` in `<head>` — works
with Google Fonts, Bunny Fonts, self-hosted `@font-face` sheets, or any
CSS that loads font-family declarations.

### Layout

```yaml
layout:
  radius: "0.5rem"            # corner radius — affects every component
  density: "comfortable"      # compact | comfortable | spacious
layout_variant: "standard"    # standard | cockpit | tiled
```

### Assets

```yaml
assets:
  bg: "https://example.com/bg.jpg"            # full-viewport background
  hero: "linear-gradient(45deg, red, blue)"   # hero illustration / pre-wrapped CSS OK
  logo: "https://example.com/logo.svg"
  crest: "..."
  sidebar: "..."
  header: "..."
  custom:
    my_key: "https://example.com/x.png"      # arbitrary custom assets
```

Every asset is emitted as a CSS variable: `--theme-asset-bg`,
`--theme-asset-hero`, `--theme-asset-custom-my_key`, etc. Bare URLs are
wrapped in `url('...')` automatically; pre-wrapped CSS expressions
(`linear-gradient(...)`, `radial-gradient(...)`, `url(...)`) pass
through unchanged.

### Color overrides

Pin specific design tokens that don't derive cleanly from the 3-layer
palette:

```yaml
color_overrides:
  primary: "#1f7a4d"
  accent: "#3d9970"
  success: "#5ba66f"
  warning: "#d4a45c"
  destructive: "#c97171"
  border: "#1d2620"
  # Plus: card, popover, secondary, muted, accent_foreground, ring, etc.
```

Both snake_case (`accent_foreground`) and camelCase (`accentForeground`)
keys are accepted — use whichever convention you're used to.

### Custom CSS

For things the schema doesn't cover (pseudo-elements, complex animations,
scoped overrides):

```yaml
custom_css: |
  .my-banner::before { content: "★"; color: var(--accent); }
  @keyframes pulse { from { opacity: 0.6; } to { opacity: 1; } }
```

Injected raw, scoped via the rendered `<style>` block. You own the scope.

### Component styles

Per-component CSS-var buckets — each entry becomes
`--component-<bucket>-<kebab-property>`:

```yaml
component_styles:
  card:
    border_color: "rgba(255, 255, 255, 0.08)"
    box_shadow: "0 4px 24px rgba(0, 0, 0, 0.3)"
  header:
    backdrop_filter: "blur(8px)"
```

The shell components read these vars with sensible defaults — your
overrides win.

---

## What gets validated

The validator catches at load time:

- `name` must be lowercase a-z, 0-9, `_`, `-`. No camelCase, no spaces.
- `palette` is required and must include `background`, `midground`,
  `foreground`. Each layer must be a valid `#RRGGBB` hex.
- `layout.density` must be one of `compact` / `comfortable` / `spacious`.
- `layout_variant` must be one of `standard` / `cockpit` / `tiled`.
- `color_overrides` keys must match the known token list (`primary`,
  `accent`, `border`, etc.). Unknown keys are rejected with a
  path-like error.
- `component_styles` buckets must match the known list (`card`,
  `header`, `footer`, `sidebar`, `tab`, `progress`, `badge`,
  `backdrop`, `page`).
- Asset `custom` keys must be alphanumeric / `_` / `-` only.

Errors are path-like so you can find them quickly:

```
ThemeValidationError: ~/.korpha/dashboard-themes/ocean.yaml:
  unknown color_overrides key 'crambo'
```

---

## Sharing a theme

A theme is one YAML file. To share:

1. Save your theme to `~/.korpha/dashboard-themes/my_theme.yaml`
2. Commit it as a [GitHub Gist](https://gist.github.com), Discord
   attachment, or paste-bin
3. Anyone can install by saving the file to *their*
   `~/.korpha/dashboard-themes/` and refreshing

There's no central registry today — distribution is intentionally
peer-to-peer. (See [`docs/THEME_CONTEST.md`](THEME_CONTEST.md) for
the community contest where shared themes can win their way into
the next Korpha release as built-ins.)

---

## Naming rules

- Themes named `default`, `midnight`, `sage`, `ember` are **built-ins** —
  user themes with those names are silently dropped from the picker
  list (built-ins always win). To customize a built-in, copy it to a
  new name like `default_warmer`.
- Filename's stem fills in `name` if you omit it — drop `twilight.yaml`
  without setting `name:` and the theme will be called `twilight`. Saves
  a line for quick personal themes.

---

## Limits

- **No code execution** — theme YAML is data only. No JavaScript, no
  Python, no template tags. (`custom_css` is plain CSS.)
- **No remote fetch** — the loader scans your local
  `~/.korpha/dashboard-themes/` only. No URL-fetch in v1 by design;
  the security model is "user explicitly placed this file."
- **One YAML per theme** — multi-file themes (separate palette /
  typography / etc.) aren't supported. Keep it self-contained.

---

## Reference

- Schema source: [`korpha/themes/types.py`](../korpha/themes/types.py)
- Built-in examples: [`korpha/themes/presets.py`](../korpha/themes/presets.py)
  (4 themes — copy any one as a starting point)
- CSS rendering: [`korpha/themes/css.py`](../korpha/themes/css.py)
- HTTP API: `GET /api/dashboard/themes`, `PUT /api/dashboard/theme`
  (see [`korpha/api/server.py`](../korpha/api/server.py))
- Tests: 36 covering schema, loader, CSS rendering, endpoints
  ([`tests/test_themes.py`](../tests/test_themes.py))

Schema is intentionally identical to Hermes-agent's v0.12 dashboard
theme system, so any YAML that runs there runs here, and vice versa.
The Cofounder Protocol roadmap (v2) lets partners ship a theme
alongside their manifest — same schema, no translation.
