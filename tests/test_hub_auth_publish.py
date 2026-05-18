"""Tests for hub auth session + publish client."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from korpha.skills_hub.hub_auth import (
    HubSession, clear_session, load_session, save_session,
)
from korpha.skills_hub.hub_publish import HubPublishError, publish_skill


@pytest.fixture
def home_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Patch the module-level path constant (computed at import time)
    import korpha.skills_hub.hub_auth as ha
    monkeypatch.setattr(ha, "_HUB_SESSION_PATH", tmp_path / ".korpha" / "hub_session.json")
    return tmp_path


def test_save_load_roundtrip(home_tmp: Path) -> None:
    s = HubSession(
        base_url="https://skills.aigenteur.com",
        cookie="signed.cookie.value",
        email="m@x.com",
    )
    save_session(s)
    loaded = load_session()
    assert loaded is not None
    assert loaded.cookie == "signed.cookie.value"
    assert loaded.email == "m@x.com"


def test_load_returns_none_when_missing(home_tmp: Path) -> None:
    assert load_session() is None


def test_load_returns_none_on_corrupt_file(home_tmp: Path) -> None:
    p = home_tmp / ".korpha" / "hub_session.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json at all")
    assert load_session() is None


def test_clear_removes_session_file(home_tmp: Path) -> None:
    save_session(HubSession(
        base_url="x", cookie="y", email="z",
    ))
    assert clear_session() is True
    assert load_session() is None
    # Second clear is a no-op (returns False)
    assert clear_session() is False


def test_save_writes_strict_perms(home_tmp: Path) -> None:
    save_session(HubSession(base_url="x", cookie="y", email="z"))
    p = home_tmp / ".korpha" / "hub_session.json"
    if os.name == "posix":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600 perms, got {oct(mode)}"


def test_cookies_dict_shape(home_tmp: Path) -> None:
    s = HubSession(base_url="x", cookie="abc", email="z")
    assert s.cookies() == {"skillshub_session": "abc"}


# ---- publish_skill ---------------------------------------------------------


class _MockResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text

    def json(self) -> dict:
        return self._json


def test_publish_201_returns_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url, json, cookies, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["cookies"] = cookies
        return _MockResponse(201, json_body={"name": json["name"], "trust_level": "community"})

    monkeypatch.setattr("korpha.skills_hub.hub_publish.httpx.post", fake_post)
    s = HubSession(base_url="https://hub.x", cookie="C", email="m@x.com")
    result = publish_skill(
        s,
        name="my.skill",
        display_name="My skill",
        description="Does a thing.",
        tags=["t1"],
    )
    assert result["name"] == "my.skill"
    assert captured["url"] == "https://hub.x/api/v1/skills"
    assert captured["cookies"] == {"skillshub_session": "C"}
    assert captured["json"]["tags"] == ["t1"]
    assert captured["json"]["license"] == "MIT"


def test_publish_401_session_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.skills_hub.hub_publish.httpx.post",
        lambda *a, **k: _MockResponse(401, text="auth"),
    )
    with pytest.raises(HubPublishError, match="session expired"):
        publish_skill(
            HubSession(base_url="x", cookie="y", email="z"),
            name="a", display_name="A", description="d",
        )


def test_publish_409_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.skills_hub.hub_publish.httpx.post",
        lambda *a, **k: _MockResponse(409, text="dup"),
    )
    with pytest.raises(HubPublishError, match="already exists"):
        publish_skill(
            HubSession(base_url="x", cookie="y", email="z"),
            name="a", display_name="A", description="d",
        )


def test_publish_429_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.skills_hub.hub_publish.httpx.post",
        lambda *a, **k: _MockResponse(429, text="too many"),
    )
    with pytest.raises(HubPublishError, match="quota"):
        publish_skill(
            HubSession(base_url="x", cookie="y", email="z"),
            name="a", display_name="A", description="d",
        )


def test_publish_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("korpha.skills_hub.hub_publish.httpx.post", boom)
    with pytest.raises(HubPublishError, match="network error"):
        publish_skill(
            HubSession(base_url="x", cookie="y", email="z"),
            name="a", display_name="A", description="d",
        )


# ---- publish list ----------------------------------------------------------


def test_publish_list_is_non_empty_and_well_formed() -> None:
    from korpha.skills._publish_list import PUBLISHABLE_SKILLS

    assert len(PUBLISHABLE_SKILLS) >= 8
    for entry in PUBLISHABLE_SKILLS:
        assert "." in entry["name"], "skill names should be dotted"
        assert entry["display_name"]
        assert entry["description"]
        assert isinstance(entry["tags"], list)
        assert entry["license"] == "MIT"
        assert entry["upstream_repo"] == "AIgenteur/aigenteur_agent"
