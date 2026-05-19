"""Tests for the Grok Imagine video backend (PR C).

The actual API endpoint depends on xAI's spec which we can't verify
without live OAuth — these tests cover the contract surface and
verify the registration path, then mock the HTTP call to exercise
the payload-building + response-parsing logic.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from korpha.video.builtins import GrokImagineBackend
from korpha.video.provider import (
    VideoGenError, VideoGenRequest, VideoGenResult,
)


def test_name_and_capabilities() -> None:
    b = GrokImagineBackend()
    assert b.name == "grok-imagine"
    caps = b.capabilities()
    assert caps.text_to_video is True
    assert caps.image_to_video is True
    assert caps.audio_driven is False
    assert "16:9" in caps.aspect_ratios
    assert caps.has_watermark is False


def test_available_false_when_no_xai_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a configured xAI OAuth state, available() must return
    False so the dispatcher routes to another backend."""
    import korpha.inference.xai_oauth as _xa
    monkeypatch.setattr(_xa, "is_configured", lambda *a, **k: False)
    b = GrokImagineBackend()
    assert b.available() is False


def test_available_true_when_xai_oauth_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import korpha.inference.xai_oauth as _xa
    monkeypatch.setattr(_xa, "is_configured", lambda *a, **k: True)
    b = GrokImagineBackend()
    assert b.available() is True


def test_generate_raises_when_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import korpha.inference.xai_oauth as _xa
    monkeypatch.setattr(_xa, "is_configured", lambda *a, **k: False)
    b = GrokImagineBackend()
    with pytest.raises(VideoGenError, match="SuperGrok auth"):
        import asyncio
        asyncio.run(b.generate(VideoGenRequest(prompt="a dragon")))


def test_payload_includes_optional_fields() -> None:
    """Build the payload + assert it round-trips the request shape."""
    import korpha.inference.xai_oauth as _xa
    fake_auth = MagicMock(access_token="fake-token-abc")

    with patch.object(_xa, "is_configured", return_value=True), \
         patch.object(_xa, "get_auth", return_value=fake_auth):
        b = GrokImagineBackend()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"url": "https://video.x.ai/abc.mp4"}

        async def fake_post(url, json, headers):
            assert url == b._ENDPOINT_URL
            assert headers["Authorization"] == "Bearer fake-token-abc"
            assert json["prompt"] == "a dragon fighting a horse"
            assert json["model"] == "grok-imagine"
            assert json["duration_seconds"] == 6.0
            assert json["aspect_ratio"] == "16:9"
            assert json["reference_image_url"] == "https://x.com/ref.jpg"
            assert json["seed"] == 42
            assert json["custom_field"] == "passthrough"
            return mock_response

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            import asyncio
            result = asyncio.run(b.generate(VideoGenRequest(
                prompt="a dragon fighting a horse",
                duration_seconds=6.0,
                aspect_ratio="16:9",
                reference_image_url="https://x.com/ref.jpg",
                seed=42,
                extra={"custom_field": "passthrough"},
            )))

        assert isinstance(result, VideoGenResult)
        assert result.video_url == "https://video.x.ai/abc.mp4"
        assert result.cost_usd == Decimal("0")
        assert result.backend_name == "grok-imagine"


def test_parses_openai_compat_data_array() -> None:
    """xAI may return `{"data": [{"url": "..."}]}` (OpenAI image-gen
    style) instead of a top-level url field. Both shapes accepted."""
    import korpha.inference.xai_oauth as _xa
    fake_auth = MagicMock(access_token="fake")

    with patch.object(_xa, "is_configured", return_value=True), \
         patch.object(_xa, "get_auth", return_value=fake_auth):
        b = GrokImagineBackend()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"url": "https://video.x.ai/xyz.mp4"}],
        }

        async def fake_post(url, json, headers):
            return mock_response

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            import asyncio
            result = asyncio.run(b.generate(VideoGenRequest(prompt="x")))

        assert result.video_url == "https://video.x.ai/xyz.mp4"


def test_error_on_4xx() -> None:
    import korpha.inference.xai_oauth as _xa
    fake_auth = MagicMock(access_token="fake")

    with patch.object(_xa, "is_configured", return_value=True), \
         patch.object(_xa, "get_auth", return_value=fake_auth):
        b = GrokImagineBackend()

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Not entitled — upgrade to Premium+"

        async def fake_post(url, json, headers):
            return mock_response

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            import asyncio
            with pytest.raises(VideoGenError, match="403"):
                asyncio.run(b.generate(VideoGenRequest(prompt="x")))


def test_error_on_missing_url_in_response() -> None:
    import korpha.inference.xai_oauth as _xa
    fake_auth = MagicMock(access_token="fake")

    with patch.object(_xa, "is_configured", return_value=True), \
         patch.object(_xa, "get_auth", return_value=fake_auth):
        b = GrokImagineBackend()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "queued"}  # no url

        async def fake_post(url, json, headers):
            return mock_response

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            import asyncio
            with pytest.raises(VideoGenError, match="no video URL"):
                asyncio.run(b.generate(VideoGenRequest(prompt="x")))


def test_registered_at_module_load() -> None:
    """Triggers korpha.skills.__init__ which calls
    register_builtin_video_providers; grok-imagine should be there."""
    import korpha.skills  # noqa: F401 — side-effect import
    from korpha.video.registry import registered_providers
    names = {p.name for p in registered_providers()}
    assert "grok-imagine" in names
    assert "hyperframes" in names  # regression — don't lose existing
    assert "heygen" in names
