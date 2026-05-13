"""code.ship_via_codex skill tests.

All subprocess calls are mocked — no real ``codex`` invocation.
Validates: arg validation, missing-binary detection, sandbox-mode
whitelist, happy-path payload shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference import InferencePool, MockProvider, ProviderAccount
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


def _ctx(session, business, founder):
    pool = InferencePool(
        providers=[MockProvider()],
        accounts=[
            ProviderAccount(
                provider_name="mock",
                auth_type=AuthType.API_KEY,
                tier_models={InferenceTier.WORKHORSE: "m"},
                api_key="x",
            )
        ],
    )
    return SkillContext(
        business=business,
        founder=founder,
        session=session,
        cost_tracker=CostTracker(pool=pool),
    )


@dataclass
class _FakeProc:
    stdout: bytes
    stderr: bytes
    returncode: int = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        pass


def _patch_codex_subprocess(stdout: str, returncode: int = 0):
    fake = _FakeProc(stdout=stdout.encode("utf-8"), stderr=b"", returncode=returncode)

    async def fake_create(*args: object, **kwargs: object) -> _FakeProc:
        return fake

    return patch("asyncio.create_subprocess_exec", side_effect=fake_create)


@pytest.mark.asyncio
async def test_skill_registered() -> None:
    skill = default_registry.get("code.ship_via_codex")
    assert skill.spec.name == "code.ship_via_codex"


@pytest.mark.asyncio
async def test_requires_prompt(session, business, founder) -> None:
    skill = default_registry.get("code.ship_via_codex")
    with pytest.raises(SkillError, match=r"requires `prompt`"):
        await skill.run(ctx=_ctx(session, business, founder), args={"prompt": ""})


@pytest.mark.asyncio
async def test_invalid_sandbox_mode_rejected(
    session, business, founder, tmp_path: Path
) -> None:
    skill = default_registry.get("code.ship_via_codex")
    with pytest.raises(SkillError, match=r"invalid sandbox_mode"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "prompt": "Do a thing",
                "cwd": str(tmp_path),
                "sandbox_mode": "yolo-full-internet",
            },
        )


@pytest.mark.asyncio
async def test_missing_codex_binary_raises(
    session, business, founder, tmp_path: Path
) -> None:
    skill = default_registry.get("code.ship_via_codex")
    with (
        patch("korpha.skills.code_deploy.shutil.which", return_value=None),
        pytest.raises(SkillError, match=r"Codex CLI not found"),
    ):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"prompt": "Add a /healthz route", "cwd": str(tmp_path)},
        )


@pytest.mark.asyncio
async def test_missing_cwd_raises(session, business, founder) -> None:
    skill = default_registry.get("code.ship_via_codex")
    with (
        patch("korpha.skills.code_deploy.shutil.which", return_value="/usr/bin/codex"),
        pytest.raises(SkillError, match=r"cwd .* does not exist"),
    ):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "prompt": "Do a thing",
                "cwd": "/no/such/path/should-fail",
            },
        )


@pytest.mark.asyncio
async def test_happy_path_returns_payload(
    session, business, founder, tmp_path: Path
) -> None:
    skill = default_registry.get("code.ship_via_codex")
    fake_stdout = (
        "Added /healthz endpoint to api/main.py.\n"
        "Wrote test in tests/test_health.py.\n"
        "1 file changed, 12 insertions(+).\n"
    )
    with (
        patch("korpha.skills.code_deploy.shutil.which", return_value="/usr/bin/codex"),
        _patch_codex_subprocess(fake_stdout),
    ):
        result = await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "prompt": "Add /healthz to api/main.py + a test.",
                "cwd": str(tmp_path),
                "sandbox_mode": "workspace-write",
            },
        )

    assert "Codex CLI dispatched" in result.summary
    assert result.payload["sandbox_mode"] == "workspace-write"
    assert "Added /healthz" in result.payload["codex_output"]
    assert result.cost_usd == 0.0
