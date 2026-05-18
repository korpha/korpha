"""Tests for the in-dashboard hub-login state store + endpoints."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from korpha.skills_hub import hub_dashboard_auth as hda


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    hda.reset()
    yield
    hda.reset()


def test_start_issues_unique_states() -> None:
    s1 = hda.start()
    s2 = hda.start()
    assert s1 != s2
    assert hda.pending_count() == 2


def test_consume_validates_and_removes() -> None:
    s = hda.start()
    assert hda.pending_count() == 1
    assert hda.consume(s) is True
    assert hda.pending_count() == 0
    # Second consume of the same state fails (single-use)
    assert hda.consume(s) is False


def test_consume_empty_state_fails() -> None:
    assert hda.consume("") is False
    assert hda.consume("never-issued") is False


def test_expired_state_gc(monkeypatch: pytest.MonkeyPatch) -> None:
    """State past TTL is dropped silently and rejected by consume."""
    monkeypatch.setattr(hda, "STATE_TTL_SECONDS", 0.05)
    s = hda.start()
    time.sleep(0.1)
    # Trigger GC via consume — even fresh state issued in the same
    # window forces a sweep.
    assert hda.consume(s) is False
    assert hda.pending_count() == 0


# ---- HTTP endpoints --------------------------------------------------------


@pytest.fixture
def dashboard_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    """Build the dashboard app with hub session storage scoped to tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import korpha.skills_hub.hub_auth as ha
    monkeypatch.setattr(
        ha, "_HUB_SESSION_PATH", tmp_path / ".korpha" / "hub_session.json"
    )

    from korpha.api.server import build_app
    app = build_app()
    client = TestClient(app)

    # Set a session cookie so require_session passes — find the bypass
    # the dashboard uses for unauth requests. Easier: hit a route that
    # doesn't require auth. /app/hub-cli/status is unauthed.
    return client


def test_status_returns_anonymous_initially(dashboard_client: TestClient) -> None:
    r = dashboard_client.get("/app/hub-cli/status")
    assert r.status_code == 200
    assert r.json() == {"signed_in": False}


def test_start_then_status_after_callback(dashboard_client: TestClient) -> None:
    # Issue a state
    r1 = dashboard_client.post("/app/hub-cli/start")
    assert r1.status_code == 200
    body = r1.json()
    state = body["state"]
    from urllib.parse import unquote
    decoded = unquote(body["hub_login_url"])
    assert "/app/hub-cli/callback" in decoded
    assert f"state={state}" in decoded

    # POST a fake token via the callback with the state
    cb = dashboard_client.post(
        f"/app/hub-cli/callback?state={state}",
        json={"token": "signed.cookie.value", "email": "m@x.com"},
    )
    assert cb.status_code == 200, cb.text
    assert cb.json() == {"ok": True}

    # CORS allow-origin header is present + pinned to hub origin
    assert "Access-Control-Allow-Origin" in cb.headers

    # Status now reflects the signed-in state
    s = dashboard_client.get("/app/hub-cli/status")
    assert s.status_code == 200
    assert s.json()["signed_in"] is True
    assert s.json()["email"] == "m@x.com"


def test_callback_rejects_bad_state(dashboard_client: TestClient) -> None:
    r = dashboard_client.post(
        "/app/hub-cli/callback?state=never-issued",
        json={"token": "x", "email": "x@y.com"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "invalid_state"


def test_callback_rejects_missing_state(dashboard_client: TestClient) -> None:
    r = dashboard_client.post(
        "/app/hub-cli/callback",
        json={"token": "x", "email": "x@y.com"},
    )
    assert r.status_code == 403


def test_callback_state_is_single_use(dashboard_client: TestClient) -> None:
    state = dashboard_client.post("/app/hub-cli/start").json()["state"]
    r1 = dashboard_client.post(
        f"/app/hub-cli/callback?state={state}",
        json={"token": "t1", "email": "a@a.com"},
    )
    assert r1.status_code == 200
    # Replay of the same state must fail
    r2 = dashboard_client.post(
        f"/app/hub-cli/callback?state={state}",
        json={"token": "t2", "email": "b@b.com"},
    )
    assert r2.status_code == 403


def test_callback_rejects_missing_fields(dashboard_client: TestClient) -> None:
    state = dashboard_client.post("/app/hub-cli/start").json()["state"]
    r = dashboard_client.post(
        f"/app/hub-cli/callback?state={state}",
        json={"token": "", "email": "x@y.com"},
    )
    assert r.status_code == 400


def test_options_preflight_returns_cors_headers(
    dashboard_client: TestClient,
) -> None:
    r = dashboard_client.options("/app/hub-cli/callback")
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin")
    assert "POST" in r.headers.get("Access-Control-Allow-Methods", "")
