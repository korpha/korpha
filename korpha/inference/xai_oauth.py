"""xAI OAuth 2.0 PKCE loopback flow + token refresh.

Authenticates against ``auth.x.ai`` so the user signs in with their
existing X Premium+ subscription and we get a bearer token for the
xAI Responses API (``https://api.x.ai/v1``). No xAI API key required;
the SuperGrok subscription's quota covers inference + X Search.

Flow:
  1. Generate PKCE verifier + challenge
  2. Open https://auth.x.ai/oauth2/auth?... in the user's browser
  3. Spin up a one-shot HTTP server on 127.0.0.1:56121
  4. User signs in at x.ai; their browser POSTs back to /callback
  5. We exchange the code for {access_token, refresh_token, expires_at}
  6. Persist to the encrypted vault keyed ``xai-oauth`` or
     ``xai-oauth:{business_unit_id}`` for per-unit subscriptions

Token refresh happens automatically when the access_token has <120s
of life left; the refreshed token gets written back so the next call
(and a parallel `aigenteur auth refresh` invocation) see it.

References (verbatim constants from Hermes' implementation, which
in turn matches xAI's OIDC discovery doc at https://auth.x.ai/.well-
known/openid-configuration):

  - client_id (public PKCE client): b1a00492-073a-47ea-816f-4c329264a828
  - scope: openid profile email offline_access grok-cli:access api:access
  - redirect_uri: http://127.0.0.1:56121/callback
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Constants — pinned to xAI's published OIDC config + the public
# Grok-CLI client that Hermes registered. Same values across all
# installs (PKCE means the client_id alone is not a secret).
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = (
    f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
)
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = (
    "openid profile email offline_access grok-cli:access api:access"
)
XAI_OAUTH_CALLBACK_HOST = "127.0.0.1"
XAI_OAUTH_CALLBACK_PORT = 56121
XAI_OAUTH_CALLBACK_PATH = "/callback"
XAI_OAUTH_REDIRECT_URI = (
    f"http://{XAI_OAUTH_CALLBACK_HOST}:{XAI_OAUTH_CALLBACK_PORT}"
    f"{XAI_OAUTH_CALLBACK_PATH}"
)

# Refresh access_token when it has <120s left. Generous margin
# because xAI's token endpoint occasionally takes 2-3s, and a 401
# mid-stream is the worst possible UX for an agent run.
_REFRESH_MARGIN_SECONDS = 120

# xAI Responses API base — what providers point at when they have a
# subscription bearer.
XAI_API_BASE = "https://api.x.ai/v1"

# Vault key prefix. Per-unit subscriptions append ``:{business_unit_id}``.
_VAULT_KEY_PREFIX = "xai-oauth"


class XaiOAuthError(RuntimeError):
    """Auth flow / refresh / token retrieval failed."""


# ---- token shape ---------------------------------------------------


@dataclass
class XaiAuth:
    """Snapshot of the OAuth state at read time."""

    access_token: str
    refresh_token: str
    expires_at: int
    """Unix-epoch seconds when access_token expires."""

    id_token: Optional[str] = None
    token_endpoint: Optional[str] = None
    """Cached OIDC token endpoint — saved at first-login so refresh
    doesn't need to re-hit discovery."""


# ---- vault persistence --------------------------------------------


def _vault_key(business_unit_id: Optional[str]) -> str:
    """Build the vault entry name. Per-unit subscriptions get a
    suffixed key so two business units can hold independent
    SuperGrok subscriptions on the same install."""
    if not business_unit_id:
        return _VAULT_KEY_PREFIX
    safe = "".join(c for c in str(business_unit_id) if c.isalnum() or c in "-_")
    return f"{_VAULT_KEY_PREFIX}.{safe}"


def _read_state(business_unit_id: Optional[str]) -> dict[str, Any]:
    """Pull the JSON blob out of the encrypted vault. Empty dict
    when not yet authenticated."""
    from korpha.secrets.store import SecretNotFound, SecretStore

    store = SecretStore()
    try:
        raw = store.get(_vault_key(business_unit_id))
    except SecretNotFound:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise XaiOAuthError(
            f"xAI vault entry not valid JSON: {exc}",
        ) from exc


def _write_state(
    state: dict[str, Any], *, business_unit_id: Optional[str],
) -> None:
    from korpha.secrets.store import SecretStore

    store = SecretStore()
    store.set(
        _vault_key(business_unit_id),
        json.dumps(state, separators=(",", ":")),
        description="xAI Grok OAuth tokens (SuperGrok subscription)",
    )


def _snapshot(state: dict[str, Any]) -> XaiAuth:
    access = state.get("access_token") or ""
    refresh = state.get("refresh_token") or ""
    if not (access and refresh):
        raise XaiOAuthError(
            "xAI vault entry missing access_token or refresh_token "
            "— sign in again via `aigenteur auth add xai-oauth`.",
        )
    return XaiAuth(
        access_token=access,
        refresh_token=refresh,
        expires_at=int(state.get("expires_at") or 0),
        id_token=state.get("id_token"),
        token_endpoint=state.get("token_endpoint"),
    )


def is_configured(business_unit_id: Optional[str] = None) -> bool:
    """True when we have stored xAI OAuth tokens. Doesn't validate
    them — providers do that lazily on first use."""
    try:
        state = _read_state(business_unit_id)
    except XaiOAuthError:
        return False
    return bool(state.get("access_token") and state.get("refresh_token"))


# ---- OIDC discovery -----------------------------------------------


def _discover_endpoints() -> dict[str, str]:
    """Fetch the OIDC discovery document. Returns the authz +
    token endpoint URLs. Cached at module level so we don't re-hit
    discovery on every refresh."""
    if _discover_endpoints._cache:  # type: ignore[attr-defined]
        return _discover_endpoints._cache  # type: ignore[attr-defined]
    try:
        resp = httpx.get(XAI_OAUTH_DISCOVERY_URL, timeout=15.0)
        resp.raise_for_status()
        doc = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise XaiOAuthError(
            f"xAI OIDC discovery failed: {type(exc).__name__}: {exc}",
        ) from exc
    endpoints = {
        "authorization_endpoint": str(doc.get("authorization_endpoint") or ""),
        "token_endpoint": str(doc.get("token_endpoint") or ""),
    }
    if not (endpoints["authorization_endpoint"] and endpoints["token_endpoint"]):
        raise XaiOAuthError(
            "xAI OIDC discovery doc missing authz or token endpoint",
        )
    _discover_endpoints._cache = endpoints  # type: ignore[attr-defined]
    return endpoints


_discover_endpoints._cache = None  # type: ignore[attr-defined]


# ---- PKCE helpers --------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    """Generate (verifier, challenge). PKCE S256 method."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest(),
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


# ---- loopback callback server -------------------------------------


@dataclass
class _CallbackResult:
    """Mutable cell the callback handler writes into; the calling
    thread reads it after wait."""

    code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None


def _make_callback_handler(
    *, expected_state: str, result: _CallbackResult, done: threading.Event,
):
    """Build a one-shot BaseHTTPRequestHandler closure."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        # Silence server access log — it's noisy during the flow and
        # confuses the user reading the terminal during sign-in.
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
            return

        def do_GET(self) -> None:  # noqa: N802
            if not self.path.startswith(XAI_OAUTH_CALLBACK_PATH):
                self.send_response(404)
                self.end_headers()
                return
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            err = qs.get("error", [""])[0]
            state = qs.get("state", [""])[0]
            code = qs.get("code", [""])[0]
            if err:
                result.error = (
                    f"{err}: {qs.get('error_description', [''])[0]}"
                )
                self._reply_html(
                    "Sign-in failed",
                    f"<p>{result.error}</p>"
                    "<p>You can close this tab and try again "
                    "in the terminal.</p>",
                )
            elif state != expected_state:
                result.error = "state mismatch (possible CSRF)"
                self._reply_html("Sign-in failed", "State mismatch.")
            elif not code:
                result.error = "missing authorization code"
                self._reply_html("Sign-in failed", "Missing code.")
            else:
                result.code = code
                result.state = state
                self._reply_html(
                    "You're signed in",
                    "<p>You can close this tab and return to your "
                    "terminal/dashboard.</p>",
                )
            done.set()

        def _reply_html(self, title: str, body_html: str) -> None:
            page = (
                "<!doctype html><html><head>"
                f"<title>{title}</title>"
                "<style>body{font-family:system-ui,sans-serif;"
                "max-width:480px;margin:80px auto;padding:0 16px;"
                "color:#222;}h2{margin-top:0;}</style></head><body>"
                f"<h2>{title}</h2>{body_html}</body></html>"
            )
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return _Handler


def _run_loopback(
    *, expected_state: str, timeout_seconds: int,
) -> _CallbackResult:
    """Run a one-shot HTTP server on the loopback port until the
    callback hits or we time out. Returns the captured code."""
    result = _CallbackResult()
    done = threading.Event()
    handler_cls = _make_callback_handler(
        expected_state=expected_state, result=result, done=done,
    )
    try:
        server = socketserver.TCPServer(
            (XAI_OAUTH_CALLBACK_HOST, XAI_OAUTH_CALLBACK_PORT),
            handler_cls,
            bind_and_activate=False,
        )
    except OSError as exc:
        raise XaiOAuthError(
            f"could not bind {XAI_OAUTH_CALLBACK_HOST}:"
            f"{XAI_OAUTH_CALLBACK_PORT} for callback: {exc}. "
            "Another OAuth flow may be in progress, or the port "
            "is taken.",
        ) from exc
    server.allow_reuse_address = True
    try:
        server.server_bind()
        server.server_activate()
    except OSError as exc:
        server.server_close()
        raise XaiOAuthError(
            f"could not start callback server: {exc}",
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        finished = done.wait(timeout=timeout_seconds)
        if not finished:
            raise XaiOAuthError(
                f"xAI sign-in timed out after {timeout_seconds}s "
                "with no callback. The browser tab may have been "
                "closed before sign-in completed.",
            )
        if result.error:
            raise XaiOAuthError(f"xAI sign-in failed: {result.error}")
        return result
    finally:
        server.shutdown()
        server.server_close()


# ---- public login + refresh ---------------------------------------


def begin_login(
    *,
    business_unit_id: Optional[str] = None,
    open_browser: bool = True,
    timeout_seconds: int = 300,
) -> XaiAuth:
    """Run the interactive OAuth flow. Blocks until the user signs in
    or the timeout elapses.

    ``open_browser=False`` is for headless / VPS deployment — the
    caller prints the URL and the operator opens it manually (via
    SSH port-forward, typically ``ssh -L 56121:127.0.0.1:56121 ...``).

    Returns the freshly-issued :class:`XaiAuth` and persists tokens
    to the vault keyed by ``business_unit_id``.
    """
    endpoints = _discover_endpoints()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    authz_url = (
        f"{endpoints['authorization_endpoint']}?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "redirect_uri": XAI_OAUTH_REDIRECT_URI,
            "scope": XAI_OAUTH_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
    )

    if open_browser:
        try:
            webbrowser.open(authz_url, new=2)
        except Exception:  # noqa: BLE001
            # If webbrowser fails (e.g. no DISPLAY), fall through
            # and let the operator copy the URL themselves.
            logger.warning(
                "could not open browser automatically; "
                "open this URL manually:\n%s",
                authz_url,
            )
    else:
        logger.info("xAI sign-in URL:\n%s", authz_url)
        print(  # noqa: T201
            f"\nOpen this URL on a machine that can reach this one "
            f"on port {XAI_OAUTH_CALLBACK_PORT}:\n\n  {authz_url}\n",
        )

    result = _run_loopback(
        expected_state=state, timeout_seconds=timeout_seconds,
    )
    assert result.code is not None  # guarded by _run_loopback

    # Exchange code for tokens.
    try:
        resp = httpx.post(
            endpoints["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": result.code,
                "client_id": XAI_OAUTH_CLIENT_ID,
                "redirect_uri": XAI_OAUTH_REDIRECT_URI,
                "code_verifier": verifier,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise XaiOAuthError(
            f"xAI token exchange transport error: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    if resp.status_code != 200:
        raise XaiOAuthError(
            f"xAI token exchange rejected ({resp.status_code}): "
            f"{resp.text[:300]}",
        )
    payload = resp.json()
    state_dict = {
        "access_token": payload.get("access_token") or "",
        "refresh_token": payload.get("refresh_token") or "",
        "id_token": payload.get("id_token"),
        "token_endpoint": endpoints["token_endpoint"],
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 0),
        "first_login_at": int(time.time()),
        "business_unit_id": business_unit_id,
    }
    if not state_dict["access_token"]:
        raise XaiOAuthError("xAI token response missing access_token")
    _write_state(state_dict, business_unit_id=business_unit_id)
    return _snapshot(state_dict)


_REFRESH_LOCK = threading.Lock()


def _refresh(
    state: dict[str, Any], *, business_unit_id: Optional[str],
) -> dict[str, Any]:
    """Hit the OAuth token endpoint to mint a new access_token. Writes
    the updated tokens back to the vault and returns the new state."""
    refresh_token = state.get("refresh_token") or ""
    token_endpoint = state.get("token_endpoint")
    if not token_endpoint:
        token_endpoint = _discover_endpoints()["token_endpoint"]
    try:
        resp = httpx.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": XAI_OAUTH_CLIENT_ID,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=20.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise XaiOAuthError(
            f"xAI refresh transport error: {type(exc).__name__}: {exc}",
        ) from exc
    if resp.status_code in (400, 401):
        raise XaiOAuthError(
            f"xAI refresh rejected ({resp.status_code}): "
            f"{resp.text[:300]}. Sign in again via "
            "`aigenteur auth add xai-oauth`.",
        )
    if resp.status_code != 200:
        raise XaiOAuthError(
            f"xAI refresh failed ({resp.status_code}): "
            f"{resp.text[:300]}",
        )
    payload = resp.json()
    new_access = payload.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise XaiOAuthError("xAI refresh response missing access_token")
    state["access_token"] = new_access
    # xAI rotates refresh tokens — accept the new one when present.
    if payload.get("refresh_token"):
        state["refresh_token"] = payload["refresh_token"]
    if payload.get("id_token"):
        state["id_token"] = payload["id_token"]
    state["expires_at"] = int(time.time()) + int(payload.get("expires_in") or 0)
    state["last_refresh"] = int(time.time())
    state["token_endpoint"] = token_endpoint
    _write_state(state, business_unit_id=business_unit_id)
    return state


def get_auth(
    business_unit_id: Optional[str] = None,
    *,
    refresh_if_needed: bool = True,
) -> XaiAuth:
    """Load OAuth state from the vault, refreshing the access_token
    when expired or within ``_REFRESH_MARGIN_SECONDS`` of expiring.

    Per-unit subscriptions: pass the business_unit_id you're operating
    on. The unit's own subscription is used when present, else the
    install-wide one, else raise.
    """
    state = _read_state(business_unit_id)
    if not state and business_unit_id:
        # Fall back to install-wide subscription.
        state = _read_state(None)
        if state:
            business_unit_id = None
    if not state:
        raise XaiOAuthError(
            "no xAI OAuth tokens — sign in via "
            "`aigenteur auth add xai-oauth`.",
        )

    auth = _snapshot(state)
    if not refresh_if_needed:
        return auth
    now = int(time.time())
    if auth.expires_at - now > _REFRESH_MARGIN_SECONDS:
        return auth

    logger.info(
        "xai_oauth: access_token expires in %ds — refreshing",
        max(0, auth.expires_at - now),
    )
    with _REFRESH_LOCK:
        # Re-read under lock in case a sibling thread refreshed first.
        state = _read_state(business_unit_id)
        auth = _snapshot(state)
        if auth.expires_at - int(time.time()) > _REFRESH_MARGIN_SECONDS:
            return auth
        state = _refresh(state, business_unit_id=business_unit_id)
    return _snapshot(state)


def logout(business_unit_id: Optional[str] = None) -> bool:
    """Forget stored tokens. Returns True if anything was removed."""
    from korpha.secrets.store import SecretStore

    store = SecretStore()
    return store.delete(_vault_key(business_unit_id))


__all__ = [
    "XAI_API_BASE",
    "XAI_OAUTH_CALLBACK_HOST",
    "XAI_OAUTH_CALLBACK_PORT",
    "XAI_OAUTH_CLIENT_ID",
    "XAI_OAUTH_DISCOVERY_URL",
    "XAI_OAUTH_ISSUER",
    "XAI_OAUTH_REDIRECT_URI",
    "XAI_OAUTH_SCOPE",
    "XaiAuth",
    "XaiOAuthError",
    "begin_login",
    "get_auth",
    "is_configured",
    "logout",
]
