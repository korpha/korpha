"""Tests for the video-gen plugin contract."""
from __future__ import annotations

from decimal import Decimal

import pytest

from korpha.video import (
    VideoGenCapabilities,
    VideoGenError,
    VideoGenProvider,
    VideoGenRequest,
    VideoGenResult,
    register_provider,
    registered_providers,
    resolve_provider,
)
from korpha.video.registry import clear_registry


class _FakeText2Video(VideoGenProvider):
    name = "fake-t2v"
    _ok: bool = True

    def capabilities(self) -> VideoGenCapabilities:
        return VideoGenCapabilities(text_to_video=True)

    def available(self) -> bool:
        return self._ok

    async def generate(self, request: VideoGenRequest) -> VideoGenResult:
        return VideoGenResult(
            video_url="https://example.com/v.mp4",
            backend_name=self.name,
            cost_usd=Decimal("0.05"),
        )


class _FakeImage2Video(VideoGenProvider):
    name = "fake-i2v"

    def capabilities(self) -> VideoGenCapabilities:
        return VideoGenCapabilities(image_to_video=True)

    async def generate(self, request: VideoGenRequest) -> VideoGenResult:
        return VideoGenResult(backend_name=self.name)


class _FakeAvatar(VideoGenProvider):
    name = "fake-avatar"

    def capabilities(self) -> VideoGenCapabilities:
        return VideoGenCapabilities(audio_driven=True)

    async def generate(self, request: VideoGenRequest) -> VideoGenResult:
        return VideoGenResult(backend_name=self.name)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def test_register_and_list_providers():
    register_provider(_FakeText2Video())
    register_provider(_FakeImage2Video())
    names = [p.name for p in registered_providers()]
    assert names == ["fake-t2v", "fake-i2v"]


def test_register_with_empty_name_raises():
    class _Bad(VideoGenProvider):
        name = ""

        def capabilities(self):
            return VideoGenCapabilities()

        async def generate(self, request):  # noqa: ARG002
            raise NotImplementedError

    with pytest.raises(ValueError, match="non-empty"):
        register_provider(_Bad())


def test_re_register_same_name_replaces():
    p1 = _FakeText2Video()
    p2 = _FakeText2Video()
    register_provider(p1)
    register_provider(p2)
    listed = list(registered_providers())
    assert len(listed) == 1
    assert listed[0] is p2


def test_resolve_by_explicit_name():
    register_provider(_FakeText2Video())
    register_provider(_FakeImage2Video())
    p = resolve_provider("fake-i2v")
    assert p.name == "fake-i2v"


def test_resolve_unknown_name_returns_none():
    register_provider(_FakeText2Video())
    assert resolve_provider("nope") is None


def test_resolve_prefers_audio_when_audio_url_set():
    register_provider(_FakeText2Video())
    register_provider(_FakeAvatar())
    p = resolve_provider(
        request=VideoGenRequest(
            prompt="hi", audio_url="https://x/audio.mp3",
        ),
    )
    assert p.name == "fake-avatar"


def test_resolve_prefers_image2video_when_ref_image_set():
    register_provider(_FakeText2Video())
    register_provider(_FakeImage2Video())
    p = resolve_provider(
        request=VideoGenRequest(
            prompt="hi", reference_image_url="https://x/img.png",
        ),
    )
    assert p.name == "fake-i2v"


def test_resolve_falls_back_to_text2video():
    register_provider(_FakeImage2Video())
    register_provider(_FakeText2Video())
    p = resolve_provider(request=VideoGenRequest(prompt="hi"))
    assert p.name == "fake-t2v"


def test_resolve_skips_unavailable_providers():
    p1 = _FakeText2Video()
    p1._ok = False
    register_provider(p1)
    register_provider(_FakeAvatar())
    p = resolve_provider(request=VideoGenRequest(prompt="hi"))
    # text2video is unavailable → falls through to avatar.
    assert p.name == "fake-avatar"


def test_resolve_empty_registry_returns_none():
    assert resolve_provider() is None
    assert resolve_provider(request=VideoGenRequest(prompt="hi")) is None


@pytest.mark.anyio
async def test_provider_generate_returns_result():
    p = _FakeText2Video()
    out = await p.generate(VideoGenRequest(prompt="cat on a skateboard"))
    assert out.video_url == "https://example.com/v.mp4"
    assert out.cost_usd == Decimal("0.05")
    assert out.backend_name == "fake-t2v"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---- built-in adapters --------------------------------------------


def test_builtin_register_idempotent():
    from korpha.video.builtins import register_builtin_video_providers

    register_builtin_video_providers()
    first = [p.name for p in registered_providers()]
    register_builtin_video_providers()
    second = [p.name for p in registered_providers()]
    assert first == second
    assert "hyperframes" in first
    assert "heygen" in first


def test_heygen_unavailable_without_env(monkeypatch):
    monkeypatch.delenv("HEYGEN_API_KEY", raising=False)
    from korpha.video.builtins import HeyGenBackend
    assert HeyGenBackend().available() is False


def test_heygen_available_with_env(monkeypatch):
    monkeypatch.setenv("HEYGEN_API_KEY", "fake-key")
    from korpha.video.builtins import HeyGenBackend
    assert HeyGenBackend().available() is True
