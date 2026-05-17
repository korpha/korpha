"""Tests for the xAI OAuth flow + Responses provider.

Covers:
  - PKCE pair generation
  - Vault persistence (per-install + per-unit)
  - Token refresh on stale access_token
  - Provider auth-error surfacing
  - is_configured() truthiness

The full loopback flow requires a real browser, so we stub the
HTTP exchange + bypass the callback server in the integration test.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from korpha.inference import xai_oauth


@pytest.fixture(autouse=True)
def temp_korpha_dir(tmp_path: Path, monkeypatch):
    """Isolate vault writes per-test."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    yield tmp_path


# ---- PKCE primitives ------------------------------------------------


def test_pkce_pair_is_url_safe_and_length_bounded():
    verifier, challenge = xai_oauth._pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert 43 <= len(challenge) <= 128
    # URL-safe alphabet only.
    safe = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(verifier).issubset(safe)
    assert set(challenge).issubset(safe)


# ---- vault round-trip ----------------------------------------------


def test_vault_key_install_wide_vs_per_unit():
    assert xai_oauth._vault_key(None) == "xai-oauth"
    assert xai_oauth._vault_key("abc-123") == "xai-oauth.abc-123"
    # Non-safe chars dropped.
    assert xai_oauth._vault_key("a/b c") == "xai-oauth.abc"


def test_vault_round_trip_install_wide():
    state = {
        "access_token": "tok_access",
        "refresh_token": "tok_refresh",
        "expires_at": int(time.time()) + 3600,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    xai_oauth._write_state(state, business_unit_id=None)
    assert xai_oauth.is_configured() is True
    auth = xai_oauth.get_auth(refresh_if_needed=False)
    assert auth.access_token == "tok_access"
    assert auth.refresh_token == "tok_refresh"


def test_vault_round_trip_per_unit():
    s1 = {
        "access_token": "tok_a",
        "refresh_token": "ref_a",
        "expires_at": int(time.time()) + 3600,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    s2 = {
        "access_token": "tok_b",
        "refresh_token": "ref_b",
        "expires_at": int(time.time()) + 3600,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    xai_oauth._write_state(s1, business_unit_id="unit-1")
    xai_oauth._write_state(s2, business_unit_id="unit-2")

    a1 = xai_oauth.get_auth("unit-1", refresh_if_needed=False)
    a2 = xai_oauth.get_auth("unit-2", refresh_if_needed=False)
    assert a1.access_token == "tok_a"
    assert a2.access_token == "tok_b"


def test_per_unit_falls_back_to_install_wide():
    state = {
        "access_token": "shared_tok",
        "refresh_token": "shared_ref",
        "expires_at": int(time.time()) + 3600,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    xai_oauth._write_state(state, business_unit_id=None)
    # No per-unit token → falls back to install-wide.
    auth = xai_oauth.get_auth("any-unit", refresh_if_needed=False)
    assert auth.access_token == "shared_tok"


def test_get_auth_raises_when_nothing_stored():
    with pytest.raises(xai_oauth.XaiOAuthError, match="no xAI OAuth tokens"):
        xai_oauth.get_auth(refresh_if_needed=False)


def test_logout_removes_tokens():
    state = {
        "access_token": "x", "refresh_token": "y",
        "expires_at": int(time.time()) + 3600,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    xai_oauth._write_state(state, business_unit_id=None)
    assert xai_oauth.is_configured() is True
    assert xai_oauth.logout() is True
    assert xai_oauth.is_configured() is False
    # Idempotent.
    assert xai_oauth.logout() is False


# ---- refresh -------------------------------------------------------


def test_refresh_triggers_when_expired(monkeypatch):
    """When access_token is within REFRESH_MARGIN of expiry, get_auth
    should call the refresh endpoint + write the new tokens back."""
    state = {
        "access_token": "old_access",
        "refresh_token": "ref_old",
        "expires_at": int(time.time()) + 30,  # 30s — under 120s margin
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    xai_oauth._write_state(state, business_unit_id=None)

    refresh_calls: list[dict] = []

    def fake_post(url, data, headers, timeout):
        refresh_calls.append({"url": url, "data": data})
        return httpx.Response(
            200,
            json={
                "access_token": "new_access",
                "refresh_token": "ref_new",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(xai_oauth.httpx, "post", fake_post)

    auth = xai_oauth.get_auth()
    assert auth.access_token == "new_access"
    assert auth.refresh_token == "ref_new"
    assert auth.expires_at > int(time.time()) + 3500
    assert len(refresh_calls) == 1
    assert refresh_calls[0]["data"]["grant_type"] == "refresh_token"

    # Persisted to vault.
    fresh = xai_oauth.get_auth(refresh_if_needed=False)
    assert fresh.access_token == "new_access"


def test_refresh_skipped_when_token_fresh(monkeypatch):
    state = {
        "access_token": "still_fresh",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 3600,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    xai_oauth._write_state(state, business_unit_id=None)

    def fake_post(*a, **k):  # noqa: ARG001
        raise AssertionError("refresh should not be called")

    monkeypatch.setattr(xai_oauth.httpx, "post", fake_post)
    auth = xai_oauth.get_auth()
    assert auth.access_token == "still_fresh"


def test_refresh_rejected_surfaces_xaioautherror(monkeypatch):
    state = {
        "access_token": "stale",
        "refresh_token": "ref",
        "expires_at": int(time.time()) - 60,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    xai_oauth._write_state(state, business_unit_id=None)

    def fake_post(url, data, headers, timeout):
        return httpx.Response(
            400, text='{"error":"invalid_grant"}',
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(xai_oauth.httpx, "post", fake_post)
    with pytest.raises(xai_oauth.XaiOAuthError, match="refresh rejected"):
        xai_oauth.get_auth()


# ---- provider integration ------------------------------------------


def test_provider_raises_when_no_oauth(monkeypatch):
    """The xai_oauth provider should surface a clean ProviderError
    when no tokens exist, not crash with an attribute error."""
    from korpha.inference.providers.xai_responses import (
        XaiResponsesProvider,
    )
    from korpha.inference.registry import ProviderAccount
    from korpha.inference.types import CompletionRequest, Message, Role
    from korpha.audit.model import InferenceTier
    from korpha.inference.provider import ProviderError

    from korpha.inference.registry import AuthType
    provider = XaiResponsesProvider()
    account = ProviderAccount(
        provider_name="xai-oauth",
        auth_type=AuthType.OAUTH,
        tier_models={InferenceTier.PRO: "grok-4.3"},
    )
    req = CompletionRequest(
        messages=[Message(role=Role.USER, content="hi")],
        tier=InferenceTier.PRO,
        session_key="test",
    )
    with pytest.raises(ProviderError, match="xAI auth"):
        import asyncio
        asyncio.run(provider.complete(req, account))
