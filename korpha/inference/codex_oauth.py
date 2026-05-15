"""Codex OAuth token reader + refresher + Cloudflare-bypass headers.

The chatgpt.com/backend-api/codex surface speaks the OpenAI Responses
API but lives behind Cloudflare's WAF. Bare httpx requests get 403'd
unless the request carries:

  - User-Agent shaped like the real codex-rs CLI
  - ``originator: codex_cli_rs`` header
  - ``ChatGPT-Account-ID`` extracted from the JWT's auth claim
  - A non-expired access token (refresh via OAuth when stale)

Mirrors Hermes's auth flow (``hermes/agent/auxiliary_client.py``
``_codex_cloudflare_headers`` and ``hermes/hermes_cli/auth.py``
``_refresh_codex_tokens_blocking``). We read from ``~/.codex/auth.json``
— the same file ``codex login`` writes — and write back refreshed
tokens so subsequent calls (and the ``codex`` CLI itself) get the
fresh access_token.

Used by:
  - korpha/inference/providers/codex_responses.py — inference
  - korpha/inference/providers/codex_responses_image.py — image gen
  - korpha/web/providers/codex_web.py — web_search tool
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Pinned to the real codex-rs OAuth client. Same value Hermes uses.
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"

# Refresh access_token when it has <60s of life left. Cloudflare returns
# 401 the moment exp passes; we'd rather pay the refresh cost than serve
# a doomed request to the agent loop.
_REFRESH_MARGIN_SECONDS = 60

# All file writes go through this so two threads / async tasks don't
# stomp on each other's refresh.
_WRITE_LOCK = threading.Lock()


def _auth_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


@dataclass
class CodexAuth:
    """Snapshot of the OAuth state at read time."""

    access_token: str
    refresh_token: str
    id_token: str | None
    account_id: str | None
    expires_at: int
    """Unix-epoch seconds when access_token claim ``exp`` expires."""


class CodexAuthError(RuntimeError):
    """Raised when token reading / refreshing fails irrecoverably.

    Callers that want graceful degradation should catch this and fall
    through to the next provider in the cascade rather than surface
    the error to the agent."""


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode the *unverified* JWT payload. We trust the file system
    write permissions (chmod 0600 on ~/.codex/auth.json) — this isn't
    a security boundary."""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("malformed JWT (missing payload segment)")
    pad = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(pad))


def _read_auth_file() -> dict[str, Any]:
    path = _auth_path()
    if not path.exists():
        raise CodexAuthError(
            f"{path} not found — run `codex login` to authenticate"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CodexAuthError(
            f"failed to parse {path}: {type(exc).__name__}: {exc}"
        ) from exc


def _write_auth_file(state: dict[str, Any]) -> None:
    path = _auth_path()
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve 0600 perms (the codex CLI sets them).
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _snapshot(state: dict[str, Any]) -> CodexAuth:
    tokens = state.get("tokens") or {}
    access = tokens.get("access_token") or ""
    refresh = tokens.get("refresh_token") or ""
    id_tok = tokens.get("id_token")
    account_id = tokens.get("account_id")
    if not access or not refresh:
        raise CodexAuthError(
            "auth.json missing access_token or refresh_token — "
            "run `codex login` to re-authenticate"
        )
    try:
        claims = _decode_jwt_claims(access)
        exp = int(claims.get("exp") or 0)
    except Exception:  # noqa: BLE001
        exp = 0
    return CodexAuth(
        access_token=access,
        refresh_token=refresh,
        id_token=id_tok if isinstance(id_tok, str) else None,
        account_id=account_id if isinstance(account_id, str) else None,
        expires_at=exp,
    )


def _refresh_tokens(refresh_token: str) -> dict[str, Any]:
    """Hit the OAuth endpoint to mint a new access_token. Returns the
    raw response JSON. Raises :class:`CodexAuthError` on any failure."""
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                },
            )
    except Exception as exc:  # noqa: BLE001
        raise CodexAuthError(
            f"codex token refresh transport error: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code != 200:
        body = ""
        try:
            body = resp.text[:400]
        except Exception:  # noqa: BLE001
            pass
        # OpenAI returns 400 with error="invalid_grant" / "refresh_token_reused"
        # when the refresh token has been rotated (e.g. by the codex CLI
        # in a separate process). User needs to `codex login` again.
        if resp.status_code in (400, 401):
            raise CodexAuthError(
                f"codex refresh rejected ({resp.status_code}): {body}. "
                "Run `codex login` to re-authenticate."
            )
        raise CodexAuthError(
            f"codex refresh failed ({resp.status_code}): {body}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise CodexAuthError(
            f"codex refresh response not JSON: {type(exc).__name__}: {exc}"
        ) from exc


def get_codex_auth(*, refresh_if_needed: bool = True) -> CodexAuth:
    """Load OAuth state from ~/.codex/auth.json, refreshing the
    access_token when it's expired (or within :data:`_REFRESH_MARGIN_SECONDS`
    of expiring).

    Persists refreshed tokens back to auth.json so the next caller
    (and the ``codex`` CLI itself) see the fresh access_token.
    """
    state = _read_auth_file()
    auth = _snapshot(state)
    if not refresh_if_needed:
        return auth
    now = int(time.time())
    if auth.expires_at - now > _REFRESH_MARGIN_SECONDS:
        return auth
    logger.info(
        "codex_oauth: access_token expires in %ds — refreshing",
        auth.expires_at - now,
    )
    payload = _refresh_tokens(auth.refresh_token)
    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token") or auth.refresh_token
    new_id = payload.get("id_token") or auth.id_token
    if not isinstance(new_access, str) or not new_access.strip():
        raise CodexAuthError("refresh response missing access_token")
    state.setdefault("tokens", {})
    state["tokens"]["access_token"] = new_access
    state["tokens"]["refresh_token"] = new_refresh
    if new_id:
        state["tokens"]["id_token"] = new_id
    state["last_refresh"] = int(time.time())
    _write_auth_file(state)
    return _snapshot(state)


def cloudflare_headers(access_token: str) -> dict[str, str]:
    """Return the headers Cloudflare needs to admit a request to
    ``chatgpt.com/backend-api/codex``. See module docstring for the why.

    Tolerates malformed tokens — drops the account-ID header rather than
    raise, so a bad token still surfaces as a clean 401 at request time
    instead of a crash at client construction.
    """
    headers: dict[str, str] = {
        "User-Agent": "codex_cli_rs/0.0.0 (Korpha Cofounder)",
        "originator": "codex_cli_rs",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    try:
        claims = _decode_jwt_claims(access_token)
        acct = (claims.get("https://api.openai.com/auth") or {}).get(
            "chatgpt_account_id"
        )
        if isinstance(acct, str) and acct:
            headers["ChatGPT-Account-ID"] = acct
    except Exception:  # noqa: BLE001
        pass
    return headers


def is_configured() -> bool:
    """Cheap check used by provider ``is_configured()`` methods.
    True when ``~/.codex/auth.json`` exists with both tokens present."""
    try:
        state = _read_auth_file()
    except CodexAuthError:
        return False
    tokens = state.get("tokens") or {}
    return bool(tokens.get("access_token") and tokens.get("refresh_token"))


__all__ = [
    "CODEX_OAUTH_CLIENT_ID",
    "CODEX_OAUTH_TOKEN_URL",
    "CodexAuth",
    "CodexAuthError",
    "cloudflare_headers",
    "get_codex_auth",
    "is_configured",
]
