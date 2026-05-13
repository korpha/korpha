"""Detect whether the coding-delegation CLIs are installed + authenticated.

The cofounder's CTO can hand work to ``claude`` (Claude Code) or ``codex``
(OpenAI Codex CLI). Both auth via the user's existing subscription
(Claude Pro / ChatGPT) — no API key needed if they've run
``claude login`` / ``codex login``.

Mike (non-technical Founder) doesn't know any of this. ``korpha init``
runs ``check_all()`` after the provider wizard and prints a status
block so he knows whether the BRIEF.md *"hands code to Codex"* beat
will work, and what one command to run if it won't.

Detection is intentionally light:

- presence: ``shutil.which`` on the binary name
- auth: best-effort check of the canonical auth-store path. We don't
  invoke the binary because that can prompt or take seconds.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DelegationStatus:
    """Snapshot of one delegation CLI's local install state."""

    name: str
    """Human label, e.g. 'Claude Code'."""

    binary: str
    """Command name we look for on PATH."""

    installed: bool
    authenticated: bool
    """True when we found a credentials file. None of the providers expose
    a verb for ``auth status`` we can rely on, so this is best-effort —
    a stale token would still report ``True``."""

    install_hint: str
    """One-line install command shown when ``installed=False``."""

    login_hint: str
    """One-line login command shown when ``installed=True`` but
    ``authenticated=False``."""

    purpose: str
    """One sentence on what this CLI does for Korpha."""


def check_claude_code() -> DelegationStatus:
    binary = "claude"
    installed = shutil.which(binary) is not None
    # Claude Code stores subscription tokens under ~/.claude/. Existence
    # of either of these is a good signal it's been logged in.
    auth_paths = [
        Path.home() / ".claude" / ".credentials.json",
        Path.home() / ".claude" / "credentials.json",
        Path.home() / ".claude" / "auth.json",
        Path.home() / ".config" / "claude" / "credentials.json",
    ]
    authenticated = installed and any(p.exists() for p in auth_paths)
    return DelegationStatus(
        name="Claude Code",
        binary=binary,
        installed=installed,
        authenticated=authenticated,
        install_hint="curl -fsSL https://claude.ai/install.sh | bash",
        login_hint="claude  # opens a browser for Claude Pro / Max login",
        purpose=(
            "Lets the CTO delegate code-writing work to Claude Code "
            "(uses your Claude subscription, no API key)."
        ),
    )


def check_codex_cli() -> DelegationStatus:
    binary = "codex"
    installed = shutil.which(binary) is not None
    auth_paths = [
        Path.home() / ".codex" / "auth.json",
        Path.home() / ".codex" / "credentials.json",
        Path.home() / ".config" / "codex" / "auth.json",
    ]
    authenticated = installed and any(p.exists() for p in auth_paths)
    return DelegationStatus(
        name="Codex CLI",
        binary=binary,
        installed=installed,
        authenticated=authenticated,
        install_hint="npm install -g @openai/codex",
        login_hint="codex login  # opens a browser for ChatGPT OAuth",
        purpose=(
            "Lets the CTO delegate code-writing work to Codex (uses "
            "your ChatGPT Plus / Pro subscription, no API key)."
        ),
    )


def check_all() -> list[DelegationStatus]:
    """Run every delegation check and return results in display order."""
    return [check_claude_code(), check_codex_cli()]


__all__ = ["DelegationStatus", "check_all", "check_claude_code", "check_codex_cli"]
