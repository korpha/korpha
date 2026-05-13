#!/usr/bin/env bash
# Korpha installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Korpha/korpha/main/install.sh | bash
#
# Designed for non-technical Founders ("Mike"). Does not require Python
# pre-installed; we'll install uv (Astral's Python toolchain) which
# handles the rest.
#
# Steps:
#   1. Refuse to run on Windows native (point to WSL).
#   2. Install uv if missing (official one-liner, signed by Astral).
#   3. uv tool install korpha from this GitHub repo (isolated venv,
#      auto-adds the `korpha` binary to PATH).
#   4. Print the next command.

set -euo pipefail

REPO="${KORPHA_REPO:-https://github.com/korpha/korpha}"
REF="${KORPHA_REF:-main}"

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*" >&2; }

# ------------------------------------------------------------ platform check

case "$(uname -s)" in
  Linux*|Darwin*) ;;
  MINGW*|MSYS*|CYGWIN*)
    red "Korpha on native Windows isn't supported yet."
    red "Use WSL2: https://learn.microsoft.com/windows/wsl/install"
    red "Then re-run this curl one-liner inside the Ubuntu shell."
    exit 1
    ;;
  *)
    red "Unsupported OS: $(uname -s). Currently we ship for macOS + Linux."
    exit 1
    ;;
esac

bold ""
bold "Installing Korpha..."
bold ""

# -------------------------------------------------------------------- uv

if ! command -v uv >/dev/null 2>&1; then
  yellow "→ uv (Python toolchain) not found. Installing from astral.sh..."
  # Astral's official installer — doesn't require sudo, drops to ~/.local/bin
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer adds uv to PATH for the *next* shell. Make it visible now.
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    red "uv install completed but the binary still isn't on PATH."
    red "Add this to your shell rc and re-run the installer:"
    red '  export PATH="$HOME/.local/bin:$PATH"'
    exit 1
  fi
  green "✓ uv installed"
else
  green "✓ uv already installed"
fi

# -------------------------------------------------------------- korpha

bold ""
bold "→ Installing korpha from ${REPO}@${REF}..."
# uv tool install creates an isolated venv per tool and symlinks the
# binary into ~/.local/bin (or the platform-equivalent uv tools dir).
# --force so re-running the installer upgrades cleanly without prompting.
uv tool install --force "git+${REPO}@${REF}" 2>&1 | grep -vE "^(Resolved|Prepared|Downloading|Built)" || true

if ! command -v korpha >/dev/null 2>&1; then
  # `uv tool dir --bin` returns where uv symlinks installed binaries.
  # Typically ~/.local/bin. If that's not on PATH yet, tell the Founder
  # exactly what one line to add to their shell rc.
  UV_TOOL_BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
  if [ -x "${UV_TOOL_BIN}/korpha" ]; then
    # Make this shell session usable immediately.
    export PATH="${UV_TOOL_BIN}:$PATH"
    yellow "korpha installed at ${UV_TOOL_BIN}/korpha."
    yellow "Add this line to your shell rc so future shells find it:"
    yellow "  export PATH=\"${UV_TOOL_BIN}:\$PATH\""
    yellow "(also: 'uv tool update-shell' will do this for you on most shells)"
    echo ""
  else
    red "korpha was installed by uv but the binary isn't visible."
    red "Try: uv tool list  (and add its bin dir to PATH)"
    exit 1
  fi
fi

green "✓ korpha installed"

# -------------------------------------------------------------- next steps

bold ""
bold "🚀 You're ready."
bold ""
echo "Run this to set up your founder profile + LLM provider (interactive):"
echo ""
green "    korpha init"
echo ""
echo "Then start the dashboard:"
echo ""
green "    korpha server"
echo ""
echo "  → opens http://localhost:8765 — answer one question, watch your"
echo "    cofounder ship a niche, landing copy, and outreach drafts."
echo ""
echo "Community: https://www.skool.com/korpha-academy-9764 (free)"
echo ""
