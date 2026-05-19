@echo off
REM Korpha installer entry point for cmd.exe (delegates to PowerShell).
REM
REM Usage:
REM   curl -fsSL -o install.cmd https://raw.githubusercontent.com/korpha/korpha/main/scripts/install.cmd
REM   install.cmd
REM
REM Or directly:
REM   curl -fsSL -o install.ps1 https://raw.githubusercontent.com/korpha/korpha/main/scripts/install.ps1
REM   powershell -ExecutionPolicy Bypass -File install.ps1
REM
REM This wrapper exists so Windows users who don't think in PowerShell
REM can copy a one-liner into cmd.exe and it Just Works. The actual
REM install logic lives in install.ps1.

setlocal

set "PS1_URL=https://raw.githubusercontent.com/korpha/korpha/main/scripts/install.ps1"
set "TMP_PS1=%TEMP%\korpha-install-%RANDOM%.ps1"

echo Downloading Korpha installer...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue';" ^
    "Invoke-WebRequest -Uri '%PS1_URL%' -OutFile '%TMP_PS1%';"
if errorlevel 1 (
    echo Failed to download installer from %PS1_URL%
    exit /b 1
)

echo Running installer...
powershell -NoProfile -ExecutionPolicy Bypass -File "%TMP_PS1%" %*
set "RC=%ERRORLEVEL%"

del "%TMP_PS1%" >nul 2>nul
exit /b %RC%
