"""Tests for imagery.generate_image — Codex CLI subscription image gen.

We monkeypatch ``shutil.which``, ``asyncio.create_subprocess_exec``, and
the ``_GENERATED_IMAGES_DIR`` so the test doesn't depend on Codex being
installed and doesn't burn a real ChatGPT quota turn.
"""
from __future__ import annotations

import asyncio

import pytest

from korpha.skills import default_registry
from korpha.skills.imagery import GenerateImageSkill
from korpha.skills.types import SkillContext, SkillError


# A fixture-built ctx using the existing conftest.py fixtures (session,
# business, founder); cost_tracker is unused for this skill (subscription-paid).
def _ctx(session, business, founder):
    from korpha.audit.model import InferenceTier
    from korpha.inference import InferencePool, MockProvider, ProviderAccount
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.registry import AuthType

    pool = InferencePool(
        providers=[MockProvider()],
        accounts=[ProviderAccount(
            provider_name="mock", auth_type=AuthType.API_KEY,
            tier_models={InferenceTier.PRO: "m"}, api_key="x",
        )],
    )
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=pool),
    )


class _FakeProc:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout, self._stderr, self.returncode = stdout, stderr, returncode

    async def communicate(self, input: bytes | None = None):
        del input
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass


def test_default_registry_has_imagery_skill() -> None:
    skill = default_registry.get("imagery.generate_image")
    assert skill.spec.name == "imagery.generate_image"
    assert "prompt" in skill.spec.parameters


@pytest.mark.asyncio
async def test_imagery_skill_returns_generated_image_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session, business, founder
) -> None:
    """Happy path: skill routes through ImageGenService → Codex provider
    (the fallback when no image_providers configured), the generated
    PNG lands in the thread's dir, skill returns its path."""
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    fake_thread = "019df391-fake"
    fake_dir = tmp_path / fake_thread
    fake_dir.mkdir()
    fake_png = fake_dir / "ig_abc123.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    # New location for the constant — lives on the provider, not the skill.
    monkeypatch.setattr(
        "korpha.imagery.providers.codex_cli_image._GENERATED_IMAGES_DIR",
        tmp_path,
    )
    # Make sure no providers.yaml is found (so the skill falls back to
    # Codex CLI directly, matching the historic out-of-box behavior).
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "no.yaml"))

    body = (
        f'{{"type":"thread.started","thread_id":"{fake_thread}"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"saved"}}\n'
        '{"type":"turn.completed","usage":{}}\n'
    ).encode()

    async def fake_exec(*_args: str, **_kwargs):
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    skill = GenerateImageSkill()
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"prompt": "a red circle on a white background"},
    )
    assert result.summary.startswith("Image saved to ")
    assert result.payload["image_path"] == str(fake_png)
    assert result.payload["byte_size"] > 0
    assert float(result.cost_usd) == 0.0  # subscription-paid
    assert "gpt-image-2" in (result.payload.get("model_used") or "")


@pytest.mark.asyncio
async def test_imagery_skill_save_to_copies_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session, business, founder
) -> None:
    """When save_to is given, the result is copied to that path."""
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    fake_thread = "tid"
    fake_dir = tmp_path / fake_thread
    fake_dir.mkdir()
    (fake_dir / "ig_x.png").write_bytes(b"PNGBYTES")
    monkeypatch.setattr(
        "korpha.imagery.providers.codex_cli_image._GENERATED_IMAGES_DIR",
        tmp_path,
    )
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "no.yaml"))

    body = (
        f'{{"type":"thread.started","thread_id":"{fake_thread}"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
    ).encode()

    async def fake_exec(*_a, **_kw):
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    save_to = tmp_path / "out" / "logo.png"
    skill = GenerateImageSkill()
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"prompt": "x", "save_to": str(save_to)},
    )
    assert save_to.exists()
    assert save_to.read_bytes() == b"PNGBYTES"
    assert result.payload["image_path"] == str(save_to)


@pytest.mark.asyncio
async def test_imagery_skill_raises_when_no_provider_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session, business, founder
) -> None:
    """No image_providers in YAML AND no codex on PATH → actionable
    error pointing at config-image-add."""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "no.yaml"))
    skill = GenerateImageSkill()
    with pytest.raises(SkillError, match=r"config image-add"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"prompt": "x"},
        )


@pytest.mark.asyncio
async def test_imagery_skill_raises_on_empty_prompt(
    session, business, founder
) -> None:
    skill = GenerateImageSkill()
    with pytest.raises(SkillError, match=r"non-empty 'prompt'"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"prompt": ""},
        )


@pytest.mark.asyncio
async def test_imagery_skill_raises_when_no_image_file_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session, business, founder
) -> None:
    """Codex completed but didn't actually write a PNG — likely a
    refusal or sandbox block. Surface as a clear error."""
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr(
        "korpha.imagery.providers.codex_cli_image._GENERATED_IMAGES_DIR",
        tmp_path,
    )
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "no.yaml"))
    body = (
        b'{"type":"thread.started","thread_id":"empty-thread"}\n'
        b'{"type":"item.completed","item":{"type":"agent_message","text":"I cannot generate that."}}\n'
    )

    async def fake_exec(*_a, **_kw):
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    skill = GenerateImageSkill()
    with pytest.raises(SkillError, match=r"no PNG was written"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"prompt": "x"},
        )
