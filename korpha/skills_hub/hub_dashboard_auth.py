"""In-dashboard hub-login state — supports the loopback magic-link flow
without dropping the user to a terminal.

Flow the dashboard drives:
  1. POST /app/hub-cli/start          → returns {state, hub_login_url}
  2. browser opens hub_login_url in a popup
  3. user enters email + clicks magic link
  4. hub's cli_login_done.html JS POSTs the session token to
     /app/hub-cli/callback?state={state}
  5. dashboard validates the state, writes ~/.korpha/hub_session.json
  6. original dashboard tab polls /app/hub-cli/status and reloads
     when signed_in flips True

This module manages step (1)+(5) state: each start() issues a fresh
random state with a 10-min TTL; consume() validates + removes it
atomically. In-process dict — fine for the single-process dashboard,
not safe for a multi-worker uvicorn deployment (would need Redis).
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

# State TTL. The user has to: open popup, enter email, click magic link
# in their inbox, return to the popup. 10 minutes is generous; matches
# the magic-link TTL on the hub side.
STATE_TTL_SECONDS = 600


@dataclass
class _Pending:
    created_at: float


_lock = threading.Lock()
_pending: dict[str, _Pending] = {}


def start() -> str:
    """Issue a fresh state token. The dashboard hands this to the browser,
    which encodes it in the cli_return URL so it survives the round-trip
    through the hub's magic-link flow."""
    state = secrets.token_urlsafe(24)
    with _lock:
        _gc_expired()
        _pending[state] = _Pending(created_at=time.monotonic())
    return state


def consume(state: str) -> bool:
    """Validate + remove the state atomically. Returns True if it was
    present and not expired. False is a hard failure — caller should
    treat it as CSRF or replay and 403."""
    if not state:
        return False
    with _lock:
        _gc_expired()
        entry = _pending.pop(state, None)
    return entry is not None


def _gc_expired() -> None:
    """Caller holds the lock. Drops entries past TTL — bounds memory
    growth even if the user abandons the flow."""
    now = time.monotonic()
    expired = [
        s for s, p in _pending.items()
        if now - p.created_at > STATE_TTL_SECONDS
    ]
    for s in expired:
        _pending.pop(s, None)


def pending_count() -> int:
    """Diagnostic — used by tests to confirm GC."""
    with _lock:
        return len(_pending)


def reset() -> None:
    """Test hook — wipes all pending state."""
    with _lock:
        _pending.clear()


__all__ = ["STATE_TTL_SECONDS", "consume", "pending_count", "reset", "start"]
