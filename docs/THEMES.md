# Themes — change how your dashboard looks

**Audience**: anyone using the Korpha dashboard.
**Time**: 10 seconds to switch, 2 minutes to make your own.

The dashboard ships with 4 built-in themes and lets you drop in your
own as a single YAML file. No code, no rebuild, no restart.

---

## Switch a theme

Look at the top bar of the dashboard. Right of the cost pill (the
`$X.XX today` badge), you'll see a small button:

> ◐ Korpha Dark

Click it. A dropdown appears with every installed theme — built-ins
plus any you've authored yourself. Each row shows:

- **A swatch** — three colors from that theme's palette so you can
  preview before you commit
- **The label** — human-readable name
- **One-line description** — what vibe the theme aims for
- **A ✓** — next to whichever one is currently active

Click any row → the dashboard reloads with the new theme applied.
Your choice persists to `~/.korpha/config.yaml`, so the next
time you open the dashboard, it remembers.

That's it. No setting menu to dig through.

---

## Built-in themes

Four ship out of the box:

| Theme | Vibe |
| --- | --- |
| **Korpha Dark** (`default`) | The original dark mode — deep slate with blue accents. What you've been looking at since you installed. |
| **Midnight** (`midnight`) | Deep indigo + violet, Inter for body, JetBrains Mono for code. Feels like coding at 1am with the lights off. |
| **Sage (RankMyAnswer)** (`sage`) | Soft sage + cream — the RankMyAnswer brand palette. Calm, grown-up, less screen-burny. |
| **Ember** (`ember`) | Warm crimson + bronze, Spectral serif, IBM Plex Mono. For when shipping feels less like coding and more like forge work. |

You can preview any of them by clicking through the picker — the
dashboard reloads in under a second, no commitment.

---

## Make your own theme (no code)

Drop a YAML file at `~/.korpha/dashboard-themes/<your-name>.yaml`
and it appears in the picker on the next refresh.

### Quickest possible custom theme (3 colors)

```bash
mkdir -p ~/.korpha/dashboard-themes/
cat > ~/.korpha/dashboard-themes/ocean.yaml << 'EOF'
name: ocean
label: "Ocean"
description: "Cool deep blues."

palette:
  background: "#0a1929"        # deepest canvas color
  midground:  "#cfe7ff"        # primary text + accent
  foreground:                  # top highlight, usually white at low alpha
    hex: "#ffffff"
    alpha: 0
EOF
```

Refresh the dashboard. "Ocean" now shows up in the picker with a
real palette swatch using the actual colors you set.

### Push it further

The full schema covers fonts, layout density, corner radius,
backgrounds, custom CSS — all optional, all in plain YAML. See
[`THEME_PROTOCOL.md`](THEME_PROTOCOL.md) for the complete reference.

A few useful additions when you're ready:

```yaml
typography:
  font_sans: "'Inter', system-ui, sans-serif"
  font_mono: "'JetBrains Mono', ui-monospace, monospace"
  font_url: "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap"

layout:
  radius: "0.625rem"
  density: "comfortable"      # compact | comfortable | spacious

color_overrides:
  primary: "#40a9d1"
  accent: "#5fbcdf"
  destructive: "#c97171"
```

---

## Share your theme

A theme is one YAML file. Three ways to share:

1. **GitHub Gist** — paste the YAML, share the gist URL. Anyone can
   download and drop into their own `~/.korpha/dashboard-themes/`.
2. **GitHub Discussions** — post your theme + screenshot in the
   `#themes` discussion category for feedback.
3. **Theme contest** — every quarter, the top 3 community-submitted
   themes ship as new built-ins in the next Korpha release with
   author credit. See [`THEME_CONTEST.md`](THEME_CONTEST.md) for
   rules + schedule.

To install someone else's shared theme: download their YAML, save
it to `~/.korpha/dashboard-themes/`, refresh the dashboard.
That's the whole install process.

---

## Troubleshooting

**The picker doesn't appear in the topbar**
— Hard-refresh (`Cmd-Shift-R` / `Ctrl-Shift-R`). The picker is part
of the shell layout; if you've cached an older page from before the
theme update, the picker doesn't render.

**My custom theme doesn't show up in the picker**
— Check the filename: must end in `.yaml` (not `.yml`). Check the
location: `~/.korpha/dashboard-themes/` (not `~/.korpha/themes/`
or anywhere else). Refresh. If still nothing, your YAML may have a
validation error — see "My theme validation error" below.

**My custom theme appears but with no swatch / weird preview**
— You're probably missing a required field. The picker still lists
your theme but a parse error means the swatch can't render. Check
that you have `name` (lowercase a-z 0-9 _ -), `label`, `description`,
and `palette` with `background` / `midground` / `foreground`.

**My theme validation error**
— Run this to see the exact problem:

```bash
cd /path/to/korpha
uv run python -c "
from korpha.themes import load_theme_by_name
print(load_theme_by_name('your-theme-name'))
"
```

The error message tells you the bad field with a path-like prefix:

```
DashboardThemesError: Theme 'ocean' (~/.korpha/dashboard-themes/ocean.yaml)
is malformed: ~/.korpha/dashboard-themes/ocean.yaml: palette.background
must be a #RRGGBB hex; got 'navy'
```

**The text is unreadable on my custom theme**
— Probably a contrast issue. Run [WebAIM contrast checker](https://webaim.org/resources/contrastchecker/)
on `palette.background` (the deepest canvas) versus `palette.midground`
(the body text). You want **≥ 4.5:1** for body copy.

**I want to revert to the default theme**
— Click the picker, select "Korpha Dark." Or edit
`~/.korpha/config.yaml` and remove the `dashboard.theme` line
entirely (default is the fallback when no theme is set).

**I shadowed a built-in by accident**
— You can't. User themes named `default` / `midnight` / `sage` / `ember`
are silently dropped from the picker — built-ins always win on name
conflict. To customize a built-in, copy your YAML to a new name like
`default_warmer.yaml`.

---

## Reference

- Author guide (full schema): [`THEME_PROTOCOL.md`](THEME_PROTOCOL.md)
- Community contest: [`THEME_CONTEST.md`](THEME_CONTEST.md)
- Built-in source code: [`korpha/themes/presets.py`](../korpha/themes/presets.py)
- Live API: `GET /api/dashboard/themes`, `PUT /api/dashboard/theme`

The theme system is feature-compatible with Hermes-agent v0.12, so
any YAML that runs in either project runs in both.
