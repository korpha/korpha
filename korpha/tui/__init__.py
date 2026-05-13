"""Korpha TUI — full-screen terminal UI for the cofounder.

Why a TUI: VPS users land in a terminal and never open a browser.
Hermes + OpenClaw both ship TUIs; Korpha needs one to match the
expected install-and-go experience. Most solopreneurs working on a
remote VPS over SSH never open a port-forwarded :8765.

Stack: Python + Textual (vs Hermes/OpenClaw's TS+Ink/pi-tui).
Single-language stack matters more than verbatim porting; the
patterns from Hermes (transport pluggability, approval modal,
streaming render) translate cleanly to Textual primitives.

v0 (this commit):
  - In-process — TUI talks directly to the existing CEO + skill
    registry + approval gate, no IPC layer.
  - Single-pane chat with streaming response render.
  - Approval modal pops above the composer when one's pending.
  - Status bar shows agent state (idle / thinking / drafting).
  - Slash commands: /help, /clear, /quit, /approvals.
  - No themes (yet), no session picker (yet).

v1 (planned):
  - Add ``/api/tui`` WebSocket route on FastAPI so the TUI can
    connect to a remote / shared agent server (same pattern Hermes
    uses with ``tui_gateway``).
  - Slash command catalog from server.
  - Theme synced with the dashboard's active theme.
"""
from korpha.tui.app import KorphaTUI, run_tui

__all__ = ["KorphaTUI", "run_tui"]
