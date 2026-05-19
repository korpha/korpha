"""Identify machine-tied credentials that need re-login on migration.

Some creds can be transferred 1:1 across machines (a Stripe API key
is just a string — works anywhere). Others are bound to the source
machine's identity and need a fresh login on the target:

  - **Codex CLI OAuth** (``~/.codex/auth.json``) — JWT-encoded with
    a Cloudflare-bound ChatGPT-Account-ID + token expiry. Survives
    short trips between machines as long as the token hasn't
    rotated, but the cleanest path is to re-run ``codex login`` on
    the target.

  - **Claude Code CLI keychain** (``~/.claude/``) — Anthropic
    binds the session to the device fingerprint; transfer
    intermittently works but breaks unpredictably. Re-login.

  - **xAI Grok OAuth** (vault key ``xai-oauth:*``) — similar PKCE
    flow shape, generally portable but safest to re-issue.

  - **Cloudflare API tokens** scoped to specific Zone IDs — work
    across machines (just config), no re-issue needed unless the
    scope changes.

  - **Stripe / Resend / GitHub / Linear API keys** — pure strings
    in providers.yaml or .env. Portable.

  - **OpenRouter free keys** — portable but share rate-limit windows
    by IP, so concurrent use from source + target competes. The
    migration recipe is to remove from source before activating
    on target.

This module captures that catalogue in one place so the manifest
builder + restore wizard + docs all stay consistent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MachineTiedCred:
    """One credential that's bound to source-machine identity.

    Fields used by:
      - manifest builder: list creds that exist on source
      - restore wizard: prompt operator to re-login + show command
      - docs: explain why this needs re-auth
    """

    name: str
    """Stable identifier — used in manifest + wizard prompts."""

    paths: tuple[str, ...]
    """Filesystem paths (relative to $HOME) to check. If any exists
    AND the cred is detected as present in the data dir or env,
    the manifest flags it."""

    reauth_command: str
    """Shell command the operator runs on the target after restore
    to re-establish the cred. Surfaced in the manifest + the restore
    wizard's interactive prompt."""

    rationale: str
    """One-line explanation of why this can't transfer cleanly.
    Shown in the wizard so the operator knows what's happening."""

    is_present: bool = False
    """Set at scan time when the credential was detected on source.
    Default False so callers construct from the static catalogue
    then update."""


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


_CATALOGUE: tuple[MachineTiedCred, ...] = (
    MachineTiedCred(
        name="codex_cli_oauth",
        paths=("~/.codex/auth.json",),
        reauth_command="codex login",
        rationale=(
            "Codex OAuth tokens are JWT-bound to a Cloudflare "
            "ChatGPT-Account-ID + have a short refresh-token TTL. "
            "Cleanest path on a new machine is a fresh `codex login`."
        ),
    ),
    MachineTiedCred(
        name="claude_code_keychain",
        paths=("~/.claude/",),
        reauth_command="claude  (then complete the in-CLI sign-in)",
        rationale=(
            "Anthropic's Claude Code keychain is tied to the source "
            "machine's device fingerprint. Transfers may work briefly "
            "but break unpredictably; re-login is the safe path."
        ),
    ),
    MachineTiedCred(
        name="xai_oauth",
        paths=("~/.korpha/vault/xai-oauth",),
        reauth_command="korpha auth add xai-oauth",
        rationale=(
            "xAI SuperGrok PKCE tokens — generally portable but the "
            "loopback re-auth is so quick (~30s) that re-issuing on "
            "the new machine is the simpler default."
        ),
    ),
)


MACHINE_TIED_CREDS: tuple[MachineTiedCred, ...] = _CATALOGUE
"""Public read-only handle on the catalogue. Update here when a new
auth flow joins the family."""


def scan_machine_tied(
    home: Path | None = None,
) -> list[MachineTiedCred]:
    """Return catalogue entries with ``is_present=True`` for ones
    detected in the source ``$HOME``. Used by the manifest builder."""
    h = home if home is not None else Path.home()
    out: list[MachineTiedCred] = []
    for cred in _CATALOGUE:
        present = any(
            (h / p[len("~/"):] if p.startswith("~/") else Path(p)).exists()
            for p in cred.paths
        )
        out.append(
            MachineTiedCred(
                name=cred.name,
                paths=cred.paths,
                reauth_command=cred.reauth_command,
                rationale=cred.rationale,
                is_present=present,
            )
        )
    return out


__all__ = [
    "MACHINE_TIED_CREDS",
    "MachineTiedCred",
    "scan_machine_tied",
]
