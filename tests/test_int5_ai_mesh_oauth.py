"""PR-INT-5 tests — OAuth CLI subprocess invoker + AI mesh skills."""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

import korpha.shared_resources.oauth_invoker as oauth_invoker
from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.shared_resources.model import (
    SharedResource, SharedResourceKind, SharedResourceUsage,
)
from korpha.shared_resources.oauth_invoker import (
    OAuthCliResult, invoke_oauth_cli,
)
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


@pytest.fixture
def unit(
    session: Session, business: Business,
) -> BusinessUnit:
    return BusinessUnitBoard(session).create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )


def _ctx(session, business, founder, unit_id=None):
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
        business_unit_id=unit_id,
    )


# ---------------------------------------------------------------------------
# OAuth CLI invoker — uses CLI_INVOKER_OVERRIDE for tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_invoker(monkeypatch: pytest.MonkeyPatch):
    """Replace the subprocess invoker with a deterministic stub."""
    def _stub(*, resource, prompt, unit_id, session):
        return OAuthCliResult(
            stdout=f"[{resource.name} stub] {prompt[:40]}",
            stderr="", exit_code=0, cli_name=resource.name,
        )
    monkeypatch.setattr(
        oauth_invoker, "CLI_INVOKER_OVERRIDE", _stub,
    )
    yield
    monkeypatch.setattr(
        oauth_invoker, "CLI_INVOKER_OVERRIDE", None,
    )


@pytest.mark.asyncio
async def test_invoke_oauth_cli_returns_stub_output(
    session: Session, business: Business, unit: BusinessUnit,
    mock_invoker,
) -> None:
    cli = SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", available_in_modes=["local"],
        config={}, is_active=True,
    )
    session.add(cli); session.commit(); session.refresh(cli)

    result = await invoke_oauth_cli(
        resource=cli, prompt="Write a haiku",
        session=session, unit_id=unit.id, skill_name="test",
    )
    assert "stub" in result.stdout
    assert result.exit_code == 0
    assert result.cli_name == "claude-code"


@pytest.mark.asyncio
async def test_invoke_oauth_cli_records_quota(
    session: Session, business: Business, unit: BusinessUnit,
    mock_invoker,
) -> None:
    """Each successful call bumps the quota counter + logs usage."""
    cli = SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.OAUTH_CLI, name="claude-code",
        label="Claude Code", available_in_modes=["local"],
        config={}, is_active=True,
        quota_window_seconds=18000, quota_limit_in_window=50,
    )
    session.add(cli); session.commit(); session.refresh(cli)

    await invoke_oauth_cli(
        resource=cli, prompt="test", session=session,
        unit_id=unit.id,
    )
    session.refresh(cli)
    assert cli.quota_calls_in_window == 1
    assert cli.quota_window_started_at is not None

    usages = list(session.exec(select(SharedResourceUsage)).all())
    assert len(usages) == 1
    assert usages[0].consumer_unit_id == unit.id


# ---------------------------------------------------------------------------
# AI mesh skills
# ---------------------------------------------------------------------------


def _register_ai_model(
    session, business, *, name: str, endpoint: str | None = None,
):
    r = SharedResource(
        business_id=business.id,
        kind=SharedResourceKind.AI_MODEL, name=name,
        label=name, endpoint=endpoint,
        config={}, is_active=True,
    )
    session.add(r); session.commit(); session.refresh(r)
    return r


@pytest.mark.asyncio
async def test_image_generate_with_registered_model(
    session: Session, business: Business, founder: Founder,
    unit: BusinessUnit,
) -> None:
    _register_ai_model(
        session, business, name="z-image-turbo",
        endpoint="https://mesh.example/image",
    )
    skill = default_registry.skills["image.generate"]
    out = await skill.run(
        ctx=_ctx(session, business, founder, unit.id),
        args={"prompt": "cat in a hat", "model": "z-image-turbo"},
    )
    assert out.payload["url"] == "https://mesh.example/image"
    assert out.payload["model"] == "z-image-turbo"


@pytest.mark.asyncio
async def test_image_generate_unknown_model_raises(
    session: Session, business: Business, founder: Founder,
    unit: BusinessUnit,
) -> None:
    skill = default_registry.skills["image.generate"]
    with pytest.raises(SkillError, match="not registered"):
        await skill.run(
            ctx=_ctx(session, business, founder, unit.id),
            args={"prompt": "x", "model": "nope"},
        )


@pytest.mark.asyncio
async def test_image_generate_attributes_usage_to_unit(
    session: Session, business: Business, founder: Founder,
    unit: BusinessUnit,
) -> None:
    _register_ai_model(session, business, name="z-image-turbo")
    skill = default_registry.skills["image.generate"]
    await skill.run(
        ctx=_ctx(session, business, founder, unit.id),
        args={"prompt": "x"},
    )
    usages = list(session.exec(select(SharedResourceUsage)).all())
    assert len(usages) == 1
    assert usages[0].consumer_unit_id == unit.id
    assert usages[0].skill_name == "image.generate"


@pytest.mark.asyncio
async def test_audio_synthesize_with_voice_clone(
    session: Session, business: Business, founder: Founder,
    unit: BusinessUnit,
) -> None:
    _register_ai_model(session, business, name="omnivoice-tts")
    skill = default_registry.skills["audio.synthesize"]
    out = await skill.run(
        ctx=_ctx(session, business, founder, unit.id),
        args={
            "text": "Hello there",
            "model": "omnivoice-tts",
            "voice": "omnivoice:cloned-andrew",
        },
    )
    assert out.payload["voice"] == "omnivoice:cloned-andrew"
    assert out.payload["duration_seconds_estimate"] >= 1


@pytest.mark.asyncio
async def test_audio_transcribe_no_endpoint_returns_stub(
    session: Session, business: Business, founder: Founder,
    unit: BusinessUnit,
) -> None:
    """When the mesh endpoint isn't configured, the skill returns a
    clearly-stub transcript so the calling agent flow doesn't crash."""
    _register_ai_model(session, business, name="whisper", endpoint=None)
    skill = default_registry.skills["audio.transcribe"]
    out = await skill.run(
        ctx=_ctx(session, business, founder, unit.id),
        args={"audio_url": "s3://x.mp3"},
    )
    assert "stub" in out.payload["transcript"]


@pytest.mark.asyncio
async def test_image_remove_background_records_usage(
    session: Session, business: Business, founder: Founder,
    unit: BusinessUnit,
) -> None:
    _register_ai_model(session, business, name="bg-removal")
    skill = default_registry.skills["image.remove_background"]
    out = await skill.run(
        ctx=_ctx(session, business, founder, unit.id),
        args={"image_url": "https://x.example/img.png"},
    )
    assert "model" in out.payload
    usages = list(session.exec(select(SharedResourceUsage)).all())
    assert len(usages) == 1


@pytest.mark.asyncio
async def test_image_generate_no_unit_context_skips_usage(
    session: Session, business: Business, founder: Founder,
) -> None:
    """When the caller has no unit context, the skill still runs but
    doesn't insert a usage row (would have a null consumer_unit_id)."""
    _register_ai_model(session, business, name="z-image-turbo")
    skill = default_registry.skills["image.generate"]
    await skill.run(
        ctx=_ctx(session, business, founder, None),
        args={"prompt": "x"},
    )
    usages = list(session.exec(select(SharedResourceUsage)).all())
    assert len(usages) == 0
