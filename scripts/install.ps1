# Korpha installer — Windows (PowerShell 5.1+ or PowerShell Core).
#
# Usage (run as a regular user, NOT as Administrator):
#   irm https://raw.githubusercontent.com/korpha/korpha/main/scripts/install.ps1 | iex
#
# Or with options (download first then invoke):
#   iwr https://raw.githubusercontent.com/korpha/korpha/main/scripts/install.ps1 -OutFile install.ps1
#   .\install.ps1 -SkipSetup
#
# What it does:
#   1. Sanity-checks git + PowerShell version
#   2. Installs uv via winget (preferred) or the Astral installer script
#   3. git clones Korpha into $env:USERPROFILE\korpha (or -Home arg)
#   4. uv sync to create the venv + install deps
#   5. korpha init (unless -SkipSetup) to bootstrap the data dir
#   6. Prints next-steps
#
# What it does NOT do:
#   - Doesn't elevate to Administrator (Korpha runs in user space)
#   - Doesn't install Playwright browsers (separate one-time step)
#   - Doesn't install the Windows Service (korpha service install — separate)
#   - Doesn't open Windows Firewall (you decide what's reachable)

[CmdletBinding()]
param(
    [switch] $SkipSetup,
    [string] $KorphaHome = "",
    [string] $Branch = "main",
    [string] $Repo = "https://github.com/korpha/korpha.git"
)

$ErrorActionPreference = "Stop"

if (-not $KorphaHome) {
    $KorphaHome = Join-Path $env:USERPROFILE "korpha"
}

# ---------------------------------------------------------------------------
# Environment hygiene — strip inherited Python env vars that could shadow
# our install (e.g., when run from an IDE-launched terminal).
# ---------------------------------------------------------------------------

if ($env:PYTHONPATH) {
    Write-Host "! Ignoring inherited PYTHONPATH" -ForegroundColor Yellow
    Remove-Item Env:PYTHONPATH
}
if ($env:PYTHONHOME) {
    Write-Host "! Ignoring inherited PYTHONHOME" -ForegroundColor Yellow
    Remove-Item Env:PYTHONHOME
}

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

function Test-Command {
    param([string] $Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Test-Command "git")) {
    Write-Host "X required tool 'git' not found." -ForegroundColor Red
    Write-Host "  Install Git for Windows from https://git-scm.com/download/win" -ForegroundColor Yellow
    exit 1
}

# Long-path support — Korpha's node_modules-style deep paths can exceed
# Windows's default 260-char limit. Best effort: enable for the current
# git config so this install doesn't choke. User can opt in globally
# via the registry separately if they want it system-wide.
& git config --global core.longpaths true 2>&1 | Out-Null

# NTFS atomicity workaround — same one the updater uses. Apply once
# globally so future `git pull` from any tool inherits it.
& git config --global windows.appendAtomically false 2>&1 | Out-Null

# ---------------------------------------------------------------------------
# uv install
# ---------------------------------------------------------------------------

if (-not (Test-Command "uv")) {
    Write-Host "-> Installing uv (Python package manager)..."
    # Prefer the official Astral PowerShell installer — it puts uv on
    # PATH in a way that survives reboots. Falls back to winget if
    # available and the script fails.
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Host "! Astral installer failed — trying winget..." -ForegroundColor Yellow
        if (Test-Command "winget") {
            & winget install --id=astral-sh.uv -e --accept-package-agreements --accept-source-agreements
        } else {
            Write-Host "X uv install failed and winget not available." -ForegroundColor Red
            Write-Host "  Install manually from https://docs.astral.sh/uv/" -ForegroundColor Yellow
            exit 1
        }
    }
    # Refresh PATH in the current shell so the rest of the script can
    # see uv. The installer modifies the persistent PATH but not this
    # process's environment.
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","User") + ";" + $env:PATH
    if (-not (Test-Command "uv")) {
        Write-Host "X uv install completed but uv not on PATH. Open a new PowerShell and re-run." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "+ uv already installed: $(& uv --version)"
}

# ---------------------------------------------------------------------------
# Clone (or pull if already cloned)
# ---------------------------------------------------------------------------

if (Test-Path (Join-Path $KorphaHome ".git")) {
    Write-Host "-> Korpha already cloned at $KorphaHome -- pulling latest"
    & git -C $KorphaHome fetch origin
    & git -C $KorphaHome checkout $Branch
    & git -C $KorphaHome pull --ff-only origin $Branch
} elseif (Test-Path $KorphaHome) {
    Write-Host "X $KorphaHome exists but isn't a git repo. Move it aside or pick a different -KorphaHome." -ForegroundColor Red
    exit 1
} else {
    Write-Host "-> Cloning Korpha into $KorphaHome ..."
    & git clone --branch $Branch $Repo $KorphaHome
}

# ---------------------------------------------------------------------------
# Deps + initial setup
# ---------------------------------------------------------------------------

Push-Location $KorphaHome
try {
    Write-Host "-> Installing Python deps with uv sync..."
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) {
        Write-Host "X uv sync failed (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }

    if (-not $SkipSetup) {
        Write-Host "-> Bootstrapping data dir (korpha init)..."
        & uv run korpha init
        if ($LASTEXITCODE -ne 0) {
            Write-Host "! 'korpha init' returned non-zero -- finish setup manually with: cd $KorphaHome; uv run korpha init" -ForegroundColor Yellow
        }
    }
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "============================================================================"
Write-Host "+ Korpha installed at $KorphaHome" -ForegroundColor Green
Write-Host "============================================================================"
Write-Host ""
Write-Host "Next steps:"
Write-Host ""
Write-Host "  1. (One-time) Install the Playwright Chromium browser for social posting:"
Write-Host "       cd $KorphaHome; uv run playwright install chromium"
Write-Host ""
Write-Host "  2. Start the dashboard (default port 8765):"
Write-Host "       cd $KorphaHome; uv run korpha server"
Write-Host ""
Write-Host "     For LAN access from other devices, bind explicitly:"
Write-Host "       uv run korpha server --host 0.0.0.0 --port 8765"
Write-Host ""
Write-Host "  3. Open the dashboard:"
Write-Host "       http://127.0.0.1:8765/"
Write-Host ""
Write-Host "  4. Update Korpha anytime:"
Write-Host "       cd $KorphaHome; uv run korpha update"
Write-Host ""
Write-Host "For Windows service auto-start, see:"
Write-Host "  korpha service --help     (coming soon)"
Write-Host ""
