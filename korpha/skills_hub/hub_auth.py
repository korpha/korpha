"""Authenticate the local CLI against skills.aigenteur.com via loopback.

Flow mirrors the xAI OAuth loopback (`korpha/inference/xai_oauth.py`)
but simpler — the hub does its own magic-link auth and hands back a
signed session cookie value via a one-shot localhost POST. The CLI
caches that cookie at ``~/.korpha/hub_session.json`` so future
``aigenteur skill publish`` calls authenticate automatically.

Why a separate auth: the hub is a public service. Anyone publishing
needs an account there (email-verified, rate-limited per-day). Reusing
the local install's auth isn't an option — the local install knows
nothing about who's signed up at the hub.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import secrets
import socket
import socketserver
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HUB_BASE_URL = "https://skills.aigenteur.com"

_HUB_SESSION_PATH = Path.home() / ".korpha" / "hub_session.json"


class HubAuthError(RuntimeError):
    """Login flow couldn't reach a state where we have a valid session."""


# ---------------------------------------------------------------------------
# Persistence — JSON blob at ~/.korpha/hub_session.json
# ---------------------------------------------------------------------------


@dataclass
class HubSession:
    """What we cache locally after a successful login."""

    base_url: str
    cookie: str
    email: str

    def cookies(self) -> dict[str, str]:
        """Cookie dict suitable for httpx ``cookies=`` kwarg."""
        return {"skillshub_session": self.cookie}


def load_session() -> HubSession | None:
    """Read the cached session if present + parseable. None if missing."""
    if not _HUB_SESSION_PATH.exists():
        return None
    try:
        raw = json.loads(_HUB_SESSION_PATH.read_text())
        return HubSession(
            base_url=raw["base_url"],
            cookie=raw["cookie"],
            email=raw["email"],
        )
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        logger.warning("hub session file unreadable: %s", exc)
        return None


def save_session(session: HubSession) -> None:
    """Persist + tighten file perms — session cookies are bearer-equiv."""
    _HUB_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _HUB_SESSION_PATH.write_text(
        json.dumps(
            {
                "base_url": session.base_url,
                "cookie": session.cookie,
                "email": session.email,
            },
            indent=2,
        )
    )
    try:
        os.chmod(_HUB_SESSION_PATH, 0o600)
    except OSError:
        pass


def clear_session() -> bool:
    """Remove the cached session. Returns True if a file was removed."""
    if _HUB_SESSION_PATH.exists():
        _HUB_SESSION_PATH.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Loopback flow — open browser, wait for the magic-link verify to POST back
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Pick an ephemeral free port — the hub's open-redirect guard
    only checks the host, not the port, so any local port works."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot POST handler. Stashes the result on the server object."""

    expected_state: str = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # quiet — would otherwise dump to stderr

    def _send_cors(self) -> None:
        """The browser POST originates from skills.aigenteur.com — give
        it CORS permission to hit our loopback."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length).decode("utf-8")
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            self.send_response(400)
            self._send_cors()
            self.end_headers()
            self.wfile.write(b"bad json")
            return

        token = str(body.get("token", "")).strip()
        email = str(body.get("email", "")).strip()
        if not token or not email:
            self.send_response(400)
            self._send_cors()
            self.end_headers()
            self.wfile.write(b"missing token/email")
            return

        # Stash on the server obj so the main thread sees it
        self.server.captured = {"token": token, "email": email}  # type: ignore[attr-defined]
        self.send_response(200)
        self._send_cors()
        self.end_headers()
        self.wfile.write(b"ok")


def begin_login(
    base_url: str = DEFAULT_HUB_BASE_URL,
    *,
    open_browser: bool = True,
    timeout_seconds: int = 600,
) -> HubSession:
    """Run the full loopback magic-link flow. Blocks until the user
    clicks the magic link (or ``timeout_seconds`` elapses).

    Returns the new ``HubSession`` and persists it to disk.
    """
    port = _pick_free_port()
    callback_url = f"http://127.0.0.1:{port}/cb"
    state = secrets.token_urlsafe(16)

    login_url = (
        f"{base_url.rstrip('/')}/login?"
        + urllib.parse.urlencode({"cli_return": callback_url, "state": state})
    )

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True
        captured: dict[str, str] | None = None

    server = _Server(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Opening browser to: {login_url}")
    print("(if it doesn't open, copy-paste the URL above)")
    print("Then enter your email + click the magic link in your inbox.")
    print(f"Waiting up to {timeout_seconds // 60} minutes…")

    if open_browser:
        try:
            webbrowser.open(login_url)
        except webbrowser.Error:
            pass  # user can copy-paste

    deadline = threading.Event()
    timer = threading.Timer(timeout_seconds, deadline.set)
    timer.daemon = True
    timer.start()

    try:
        while server.captured is None and not deadline.is_set():
            deadline.wait(0.5)
    finally:
        timer.cancel()
        server.shutdown()
        server.server_close()

    if server.captured is None:
        raise HubAuthError(
            f"timed out after {timeout_seconds}s waiting for magic link"
        )

    session = HubSession(
        base_url=base_url.rstrip("/"),
        cookie=server.captured["token"],
        email=server.captured["email"],
    )
    save_session(session)
    return session


__all__ = [
    "DEFAULT_HUB_BASE_URL",
    "HubAuthError",
    "HubSession",
    "begin_login",
    "clear_session",
    "load_session",
    "save_session",
]
