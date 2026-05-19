#!/usr/bin/env bash
# Korpha installer — Linux + macOS.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/korpha/korpha/main/scripts/install.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --skip-setup
#
# What it does:
#   1. Sanity-checks shell + git + curl availability
#   2. Strips inherited PYTHONPATH / PYTHONHOME (defends against parent-process
#      Python tools shadowing the install)
#   3. Installs uv if not already present
#   4. git clones korpha into ~/korpha (or $KORPHA_HOME if set)
#   5. uv sync to create the venv + install deps
#   6. korpha init (unless --skip-setup) to bootstrap the data dir
#   7. Prints next-steps: how to start the server, where the dashboard lives
#
# What it does NOT do:
#   - Doesn't install Playwright browsers (that's a one-time `playwright
#     install chromium` step the user runs after — surfaced as a hint)
#   - Doesn't write systemd units (that's `korpha service install`, separate)
#   - Doesn't open firewall ports (you decide what's reachable)

set -euo pipefail

SKIP_SETUP=0
KORPHA_HOME="${KORPHA_HOME:-$HOME/korpha}"
KORPHA_REPO="${KORPHA_REPO:-https://github.com/korpha/korpha.git}"
KORPHA_BRANCH="${KORPHA_BRANCH:-main}"

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-setup) SKIP_SETUP=1; shift ;;
        --home) KORPHA_HOME="$2"; shift 2 ;;
        --branch) KORPHA_BRANCH="$2"; shift 2 ;;
        --repo) KORPHA_REPO="$2"; shift 2 ;;
        --help|-h)
            cat <<EOF
Usage: install.sh [--skip-setup] [--home PATH] [--branch NAME] [--repo URL]

  --skip-setup     Don't run \`korpha init\` after install (do it manually later)
  --home PATH      Where to clone Korpha (default: ~/korpha or \$KORPHA_HOME)
  --branch NAME    Git branch to checkout (default: main)
  --repo URL       Git remote URL (default: https://github.com/korpha/korpha.git)
EOF
            exit 0
            ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Environment hygiene
# ---------------------------------------------------------------------------

# A pre-set PYTHONPATH / PYTHONHOME from a parent Python tool can force
# pip/entrypoints to import a different checkout than the one we're
# installing here. Strip them so the install is hermetic.
if [ -n "${PYTHONPATH:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONPATH to avoid module shadowing"
    unset PYTHONPATH
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONHOME"
    unset PYTHONHOME
fi

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "✗ required tool '$1' not found on PATH" >&2
        exit 1
    }
}

need git
need curl

# ---------------------------------------------------------------------------
# uv install
# ---------------------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    echo "→ Installing uv (Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer drops uv into ~/.local/bin or similar — make sure
    # this shell can see it for the rest of the script.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        echo "✗ uv install completed but uv not on PATH. Open a new shell and re-run." >&2
        exit 1
    fi
else
    echo "✓ uv already installed: $(uv --version)"
fi

# ---------------------------------------------------------------------------
# Clone (or pull if already cloned)
# ---------------------------------------------------------------------------

if [ -d "$KORPHA_HOME/.git" ]; then
    echo "→ Korpha already cloned at $KORPHA_HOME — pulling latest"
    git -C "$KORPHA_HOME" fetch origin
    git -C "$KORPHA_HOME" checkout "$KORPHA_BRANCH"
    git -C "$KORPHA_HOME" pull --ff-only origin "$KORPHA_BRANCH"
elif [ -d "$KORPHA_HOME" ]; then
    echo "✗ $KORPHA_HOME exists but isn't a git repo. Move it aside or pick a different --home." >&2
    exit 1
else
    echo "→ Cloning Korpha into $KORPHA_HOME ..."
    git clone --branch "$KORPHA_BRANCH" "$KORPHA_REPO" "$KORPHA_HOME"
fi

# ---------------------------------------------------------------------------
# Deps + initial setup
# ---------------------------------------------------------------------------

cd "$KORPHA_HOME"

echo "→ Installing Python deps with uv sync..."
uv sync --frozen

if [ "$SKIP_SETUP" -eq 0 ]; then
    echo "→ Bootstrapping data dir (korpha init)..."
    # `korpha init` is interactive; we run it via `uv run` so it uses
    # the venv we just synced.
    uv run korpha init || {
        echo "⚠ \`korpha init\` returned non-zero — finish setup manually with: cd $KORPHA_HOME && uv run korpha init" >&2
    }
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

cat <<EOF

============================================================================
✓ Korpha installed at $KORPHA_HOME
============================================================================

Next steps:

  1. (One-time) Install the Playwright Chromium browser for social posting:
       cd $KORPHA_HOME && uv run playwright install chromium

  2. Start the dashboard (default port 8765):
       cd $KORPHA_HOME && uv run korpha server

     For LAN access from other devices, bind explicitly:
       uv run korpha server --host 0.0.0.0 --port 8765

  3. Open the dashboard:
       http://127.0.0.1:8765/

  4. Update Korpha anytime:
       cd $KORPHA_HOME && uv run korpha update

For systemd auto-start (Linux), see:
  korpha service --help     (coming soon)

EOF
