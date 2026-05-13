# Pinokio package for Korpha

[Pinokio](https://pinokio.computer) is a 1-click installer for AI
tools. Hermes Mod, ComfyUI, Stable Diffusion etc. all distribute
this way. Korpha joins them.

## What this directory contains

```
pinokio.json    ← package manifest (title, icon, menu items)
install.json    ← runs on first install: installs uv + korpha tool
start.json      ← starts the dashboard, opens browser at localhost:8765
configure.json  ← runs the provider wizard (korpha config)
doctor.json     ← runs korpha doctor (health check)
uninstall.json  ← removes the tool (preserves ~/.korpha state)
icon.png        ← package icon (TODO: add 256x256 PNG)
```

## Publishing

1. Create a public repo at `github.com/Korpha/pinokio-korpha`
2. Copy this directory's contents to its root
3. Add a 256x256 `icon.png` (the Korpha "A" mark)
4. Pinokio submission: open Pinokio → "Discover" → "Submit a script"
   and paste the GitHub repo URL
5. Pinokio team reviews + lists in their discover catalog within a
   few days

## Users install with

In Pinokio Computer:
- Click "Discover"
- Search "Korpha"
- Click "Download"
- Click "Install"

That's it. No terminal. No `curl … | bash`. No Python knowledge needed.

The install script:
1. Installs `uv` (Astral's Python toolchain) into a Pinokio-managed dir
2. Installs `korpha` via `uv tool install` from this repo's main
3. Runs `korpha init` non-interactively to bootstrap the DB
4. Opens the docs in the browser

After install, the menu shows:
- **Start dashboard** — runs `korpha server`, opens localhost:8765
- **Configure provider** — runs `korpha config` interactively
- **Doctor** — runs `korpha doctor` for health check
- **Uninstall** — removes the tool (preserves `~/.korpha`)

## Testing locally

```bash
# In Pinokio app:
# 1. Settings → Developer Mode → enable
# 2. Discover → Manage → "Add custom" → paste this dir's path
# 3. Run install
```

## Maintenance

Each Korpha release tagged in the main repo can be pinned in
`install.json` by changing `@main` to `@v0.1.0`. For now we track
`main` for fast-moving alpha releases.
