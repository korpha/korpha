"""Tests for creative.heygen_avatar, creative.hyperframes, and
marketing.video_from_post.

Heavy use of monkeypatch — the skills shell out to httpx (HeyGen API)
and subprocess (HyperFrames CLI), neither of which we can hit in CI.
We mock at the boundary: stub the HTTP client + the
asyncio.create_subprocess_exec call. The skill logic above those
boundaries is what these tests cover.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from korpha.inference.types import CompletionResponse
from korpha.skills import default_registry
from korpha.skills.creative import (
    HeyGenAvatarSkill,
    HyperFramesSkill,
)
from korpha.skills.marketing import VideoFromPostSkill
from korpha.skills.types import SkillContext, SkillError


# ---- shared fixtures ----


@pytest.fixture
def fake_ctx(session, founder, business, tmp_path: Path) -> SkillContext:
    """A SkillContext with a stub cost_tracker that returns canned
    LLM responses. Tests override .complete via monkeypatch when they
    need a specific reply."""

    class _Tracker:
        async def complete(
            self, request, *, session, business_id,
            agent_role_id=None, **_,
        ) -> CompletionResponse:
            return CompletionResponse(
                content=(
                    '{"title":"Test","script":"Hello world this is a '
                    'real script that gets spoken by the avatar.",'
                    '"hook":"Hello","cta":"Click"}'
                ),
                tool_calls=(),
                input_tokens=100,
                output_tokens=50,
                cached_tokens=0,
                cost_usd=Decimal("0.001"),
                provider="mock",
                model="mock-pro",
                account_id="mock-account",
                reasoning=None,
                finish_reason="stop",
            )

    ctx = SkillContext(
        business=business,
        founder=founder,
        session=session,
        cost_tracker=_Tracker(),
    )
    # Stash workspace on ctx (HyperFrames reads it via getattr default)
    ctx.workspace = tmp_path  # type: ignore[attr-defined]
    return ctx


# ---- creative.heygen_avatar ----


def test_heygen_skill_registered() -> None:
    assert "creative.heygen_avatar" in default_registry.skills


@pytest.mark.asyncio
async def test_heygen_requires_api_key(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEYGEN_API_KEY", raising=False)
    skill = HeyGenAvatarSkill()
    with pytest.raises(SkillError, match="HEYGEN_API_KEY"):
        await skill.run(
            ctx=fake_ctx,
            args={"script": "hi", "avatar_id": "a", "voice_id": "v"},
        )


@pytest.mark.asyncio
async def test_heygen_requires_args(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEYGEN_API_KEY", "fake")
    skill = HeyGenAvatarSkill()
    with pytest.raises(SkillError, match="requires"):
        await skill.run(ctx=fake_ctx, args={"script": "hi"})


@pytest.mark.asyncio
async def test_heygen_rejects_bad_ratio(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEYGEN_API_KEY", "fake")
    skill = HeyGenAvatarSkill()
    with pytest.raises(SkillError, match="ratio"):
        await skill.run(
            ctx=fake_ctx,
            args={
                "script": "hi", "avatar_id": "a", "voice_id": "v",
                "ratio": "4:3",
            },
        )


@pytest.mark.asyncio
async def test_heygen_happy_path(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock the HTTP roundtrip: submit returns a video_id, poll
    returns completed + a video_url. Skill should return a
    SkillResult carrying the URL + duration."""
    monkeypatch.setenv("HEYGEN_API_KEY", "fake")

    class _MockResp:
        def __init__(self, status: int, body: dict) -> None:
            self.status_code = status
            self._body = body
            self.text = str(body)

        def json(self) -> dict:
            return self._body

    class _MockClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            self._calls = 0

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, path: str, **_: object) -> _MockResp:
            return _MockResp(200, {"data": {"video_id": "vid-123"}})

        async def get(self, path: str, **_: object) -> _MockResp:
            self._calls += 1
            # First call returns processing, second returns completed
            if self._calls == 1:
                return _MockResp(200, {"data": {"status": "processing"}})
            return _MockResp(200, {
                "data": {
                    "status": "completed",
                    "video_url": "https://heygen.example/vid-123.mp4",
                    "credits_used": 5,
                    "duration": 12.4,
                }
            })

    monkeypatch.setattr(
        "korpha.skills.creative.httpx.AsyncClient", _MockClient,
    )
    # Also shrink the poll interval so the test isn't slow.
    monkeypatch.setattr(
        "korpha.skills.creative._HEYGEN_POLL_INTERVAL", 0.0,
    )

    skill = HeyGenAvatarSkill()
    result = await skill.run(
        ctx=fake_ctx,
        args={
            "script": "Hi there.",
            "avatar_id": "avatar-7",
            "voice_id": "voice-3",
        },
    )
    assert result.payload["video_url"] == "https://heygen.example/vid-123.mp4"
    assert result.payload["video_id"] == "vid-123"
    assert result.payload["duration_seconds"] == 12.4


@pytest.mark.asyncio
async def test_heygen_surfaces_render_failure(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEYGEN_API_KEY", "fake")

    class _MockResp:
        def __init__(self, status: int, body: dict) -> None:
            self.status_code = status
            self._body = body
            self.text = str(body)

        def json(self) -> dict:
            return self._body

    class _MockClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: object, **__: object) -> _MockResp:
            return _MockResp(200, {"data": {"video_id": "vid-fail"}})

        async def get(self, *_: object, **__: object) -> _MockResp:
            return _MockResp(200, {
                "data": {"status": "failed", "error": "voice rejected"},
            })

    monkeypatch.setattr(
        "korpha.skills.creative.httpx.AsyncClient", _MockClient,
    )
    monkeypatch.setattr(
        "korpha.skills.creative._HEYGEN_POLL_INTERVAL", 0.0,
    )

    skill = HeyGenAvatarSkill()
    with pytest.raises(SkillError, match="failed"):
        await skill.run(
            ctx=fake_ctx,
            args={"script": "x", "avatar_id": "a", "voice_id": "v"},
        )


# ---- creative.hyperframes ----


def test_hyperframes_skill_registered() -> None:
    assert "creative.hyperframes" in default_registry.skills


@pytest.mark.asyncio
async def test_hyperframes_clean_error_when_deps_missing(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When hyperframes / ffmpeg / node aren't on PATH, the skill
    must raise a SkillError naming the missing binaries + install
    instructions. Mike-non-technical rule depends on this."""
    monkeypatch.setattr(
        "korpha.skills.creative.shutil.which",
        lambda _: None,
    )
    skill = HyperFramesSkill()
    with pytest.raises(SkillError, match="missing"):
        await skill.run(
            ctx=fake_ctx,
            args={"avatar_clip": "https://example/x.mp4"},
        )


@pytest.mark.asyncio
async def test_hyperframes_rejects_bad_kind(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korpha.skills.creative.shutil.which",
        lambda _: "/usr/bin/fake",
    )
    skill = HyperFramesSkill()
    with pytest.raises(SkillError, match="kind"):
        await skill.run(
            ctx=fake_ctx,
            args={
                "avatar_clip": "x.mp4",
                "kind": "not_a_kind",
            },
        )


@pytest.mark.asyncio
async def test_hyperframes_requires_avatar_clip(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korpha.skills.creative.shutil.which",
        lambda _: "/usr/bin/fake",
    )
    skill = HyperFramesSkill()
    with pytest.raises(SkillError, match="avatar_clip"):
        await skill.run(ctx=fake_ctx, args={})


@pytest.mark.asyncio
async def test_hyperframes_happy_path(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """All deps present, subprocess returns 0 + writes an output
    file → SkillResult carries the path + size."""
    monkeypatch.setattr(
        "korpha.skills.creative.shutil.which",
        lambda _: "/usr/bin/fake",
    )

    # Capture the output_path the skill chooses + write a fake MP4
    # there so the .exists() + .stat() checks pass.
    captured_cmd: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok", b""

    async def _fake_exec(*cmd: str, **_: object) -> _FakeProc:
        captured_cmd.append(list(cmd))
        # The output path is the second-to-last arg (cmd shape:
        # ["hyperframes","render","--input",X,"--output",PATH,FLAG]).
        # We just look for --output and write a fake mp4 to whatever
        # follows it.
        if "--output" in cmd:
            out = Path(cmd[cmd.index("--output") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake-mp4-bytes-01234567890")
        return _FakeProc()

    monkeypatch.setattr(
        "korpha.skills.creative.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    skill = HyperFramesSkill()
    result = await skill.run(
        ctx=fake_ctx,
        args={
            "avatar_clip": "https://example/x.mp4",
            "kind": "social_ad",
            "title": "Test Title",
        },
    )
    assert result.payload["kind"] == "social_ad"
    assert result.payload["size_bytes"] > 0
    assert Path(result.payload["output_path"]).exists()
    # Skill should have invoked hyperframes with --input + --output args
    assert captured_cmd
    assert "hyperframes" in captured_cmd[0][0]
    assert "--output" in captured_cmd[0]


@pytest.mark.asyncio
async def test_hyperframes_surfaces_subprocess_failure(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korpha.skills.creative.shutil.which",
        lambda _: "/usr/bin/fake",
    )

    class _FailProc:
        returncode = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"hyperframes: bad input"

    async def _fake_exec(*_, **__) -> _FailProc:
        return _FailProc()

    monkeypatch.setattr(
        "korpha.skills.creative.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    skill = HyperFramesSkill()
    with pytest.raises(SkillError, match="render failed"):
        await skill.run(
            ctx=fake_ctx,
            args={
                "avatar_clip": "x.mp4",
                "kind": "social_ad",
            },
        )


# ---- marketing.video_from_post (chain skill) ----


def test_video_from_post_registered() -> None:
    assert "marketing.video_from_post" in default_registry.skills


@pytest.mark.asyncio
async def test_video_from_post_requires_args(
    fake_ctx: SkillContext,
) -> None:
    skill = VideoFromPostSkill()
    with pytest.raises(SkillError, match="requires"):
        await skill.run(ctx=fake_ctx, args={"source": "x"})


@pytest.mark.asyncio
async def test_video_from_post_rejects_bad_duration(
    fake_ctx: SkillContext,
) -> None:
    skill = VideoFromPostSkill()
    with pytest.raises(SkillError, match="duration_seconds"):
        await skill.run(
            ctx=fake_ctx,
            args={
                "source": "x", "avatar_id": "a", "voice_id": "v",
                "duration_seconds": 999,
            },
        )


@pytest.mark.asyncio
async def test_video_from_post_chains_through_creative_skills(
    fake_ctx: SkillContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end with mocked HeyGen + HyperFrames. Asserts the chain
    dispatches in the right order, threads the avatar URL forward,
    and aggregates costs."""
    heygen_calls: list[dict] = []
    hf_calls: list[dict] = []

    async def _fake_heygen_run(self, *, ctx, args):
        heygen_calls.append(args)
        from korpha.skills.types import SkillResult
        return SkillResult(
            skill_name="creative.heygen_avatar",
            summary="rendered",
            payload={
                "video_url": "https://heygen.example/x.mp4",
                "video_id": "vid-1",
                "duration_seconds": 30.0,
                "ratio": "16:9",
            },
            cost_usd=0.0,
        )

    async def _fake_hf_run(self, *, ctx, args):
        hf_calls.append(args)
        from korpha.skills.types import SkillResult
        return SkillResult(
            skill_name="creative.hyperframes",
            summary="composed",
            payload={
                "output_path": "/tmp/fake.mp4",
                "kind": args.get("kind"),
                "size_bytes": 1234,
                "title": args.get("title"),
            },
            cost_usd=0.0,
        )

    monkeypatch.setattr(HeyGenAvatarSkill, "run", _fake_heygen_run)
    monkeypatch.setattr(HyperFramesSkill, "run", _fake_hf_run)

    skill = VideoFromPostSkill()
    result = await skill.run(
        ctx=fake_ctx,
        args={
            "source": "Long blog post text about why solopreneurs win.",
            "avatar_id": "av-1",
            "voice_id": "vc-1",
            "duration_seconds": 30,
            "kind": "launch_reel",
            "brand_color_hex": "#ff8800",
        },
    )

    assert len(heygen_calls) == 1
    assert heygen_calls[0]["avatar_id"] == "av-1"
    assert heygen_calls[0]["voice_id"] == "vc-1"
    # The script the LLM produced should have been passed through
    assert "Hello world" in heygen_calls[0]["script"] or len(
        heygen_calls[0]["script"]
    ) > 0

    assert len(hf_calls) == 1
    assert hf_calls[0]["avatar_clip"] == "https://heygen.example/x.mp4"
    assert hf_calls[0]["kind"] == "launch_reel"
    assert hf_calls[0]["brand_color_hex"] == "#ff8800"

    assert result.payload["output_path"] == "/tmp/fake.mp4"
    assert result.payload["title"]
    assert result.payload["kind"] == "launch_reel"
    # Cost from the LLM script call (the heygen + hf mocks return 0)
    assert result.cost_usd > 0
