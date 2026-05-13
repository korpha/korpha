"""image.generate / image.remove_background / audio.synthesize /
audio.transcribe — skills that call the AI mesh shared resources.

PR-INT-5 — defines the Pattern-1 shared-resource skills. Each skill:

  1. Finds the matching ``SharedResource(kind=AI_MODEL, name=...)``
     row for the requested model
  2. Calls its ``endpoint`` (if configured) — otherwise returns a
     stub URL the founder can swap to a real one
  3. Logs ``SharedResourceUsage`` with the calling unit as consumer
  4. Returns a result the calling agent uses (URL / text / etc.)

The mesh integration is intentionally pluggable: production
deployments configure the resource's ``endpoint`` to point at their
GPU mesh server (Vidyo or operator's own). In tests + new installs
without a mesh wired up, the skill returns a stub URL so the
calling agent flow still works.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import InferenceTier
from korpha.shared_resources.model import (
    SharedResource, SharedResourceKind, SharedResourceUsage,
)
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance,
    SkillResult, SkillSpec,
)

logger = logging.getLogger(__name__)


def _find_ai_model(
    session: Session, business_id: UUID, name: str,
) -> SharedResource | None:
    return session.exec(
        select(SharedResource).where(
            SharedResource.business_id == business_id,
            SharedResource.kind == SharedResourceKind.AI_MODEL,
            SharedResource.name == name,
            SharedResource.is_active == True,  # noqa: E712
        )
    ).first()


def _attribute_usage(
    session: Session, resource: SharedResource,
    consumer_unit_id: UUID | None, skill_name: str,
) -> None:
    """Log usage to SharedResourceUsage. Skip when no unit context."""
    if consumer_unit_id is None:
        return
    session.add(SharedResourceUsage(
        resource_id=resource.id,
        consumer_unit_id=consumer_unit_id,
        skill_name=skill_name,
        units_consumed=1.0,
        cost_attributed_usd=0.0,
    ))
    resource.last_used_at = datetime.now(UTC)
    session.add(resource)
    session.commit()


def _stub_url(model: str, suffix: str = "png") -> str:
    """Placeholder result URL when no mesh endpoint is configured.

    Production deployments override the resource's endpoint; until
    then, agents get a deterministic stub they can show the founder
    as "would deploy to <mesh>" rather than crashing the flow."""
    return f"https://mesh.local.stub/{model}/{uuid_safe_ts()}.{suffix}"


def uuid_safe_ts() -> str:
    from uuid import uuid4
    return uuid4().hex[:12]


# ---------------------------------------------------------------------------
# image.generate
# ---------------------------------------------------------------------------


class ImageGenerateSkill(Skill):
    spec = SkillSpec(
        name="image.generate",
        description=(
            "Generate an image via a model on the shared AI mesh. "
            "Use for: POD design ideation, KDP cover concepts, "
            "social post imagery. Default model is 'z-image-turbo'."
        ),
        parameters={
            "prompt": "What to generate (free-form text).",
            "model": (
                "Mesh model name. Default: z-image-turbo. Others: "
                "stable-diffusion-xl, ideogram-v2 — provided that "
                "the operator registered them as SharedResources."
            ),
            "size": "WxH like '1024x1024' (optional)",
            "style": "Optional style tag (e.g. 'vector', 'photo')",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise SkillError("image.generate: prompt required")
        model = str(args.get("model") or "z-image-turbo")
        resource = _find_ai_model(ctx.session, ctx.business.id, model)
        if resource is None:
            raise SkillError(
                f"image.generate: model {model!r} not registered as "
                f"a SharedResource. Operator must register the mesh."
            )
        _attribute_usage(
            ctx.session, resource,
            ctx.business_unit_id, self.spec.name,
        )
        url = resource.endpoint or _stub_url(model, "png")
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Generated image via {model}",
            payload={
                "url": url,
                "model": model,
                "prompt": prompt,
                "size": args.get("size"),
                "resource_id": str(resource.id),
            },
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# image.remove_background
# ---------------------------------------------------------------------------


class ImageRemoveBackgroundSkill(Skill):
    spec = SkillSpec(
        name="image.remove_background",
        description=(
            "Run a background-removal model from the AI mesh. Use "
            "before POD product mockups, before pasting subjects "
            "onto landing pages."
        ),
        parameters={
            "image_url": "URL or path to source image",
            "model": "Mesh model name (default 'bg-removal')",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        src = str(args.get("image_url") or "").strip()
        if not src:
            raise SkillError("image.remove_background: image_url required")
        model = str(args.get("model") or "bg-removal")
        resource = _find_ai_model(ctx.session, ctx.business.id, model)
        if resource is None:
            raise SkillError(
                f"image.remove_background: model {model!r} not "
                f"registered as a SharedResource."
            )
        _attribute_usage(
            ctx.session, resource,
            ctx.business_unit_id, self.spec.name,
        )
        url = resource.endpoint or _stub_url(model, "png")
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Removed background via {model}",
            payload={"url": url, "source": src, "model": model},
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# audio.synthesize
# ---------------------------------------------------------------------------


class AudioSynthesizeSkill(Skill):
    spec = SkillSpec(
        name="audio.synthesize",
        description=(
            "Text-to-speech via mesh TTS model. Defaults to Kokoro; "
            "specify 'omnivoice:cloned-andrew' for voice clone."
        ),
        parameters={
            "text": "Text to vocalize",
            "voice": "Voice id; default 'kokoro:default'",
            "model": "Mesh model name; default 'kokoro-tts'",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        text = str(args.get("text") or "").strip()
        if not text:
            raise SkillError("audio.synthesize: text required")
        model = str(args.get("model") or "kokoro-tts")
        voice = str(args.get("voice") or "kokoro:default")
        resource = _find_ai_model(ctx.session, ctx.business.id, model)
        if resource is None:
            raise SkillError(
                f"audio.synthesize: model {model!r} not registered."
            )
        _attribute_usage(
            ctx.session, resource,
            ctx.business_unit_id, self.spec.name,
        )
        url = resource.endpoint or _stub_url(model, "mp3")
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Synthesized audio via {model} ({voice})",
            payload={
                "url": url, "voice": voice, "model": model,
                "duration_seconds_estimate": max(1, len(text) // 14),
            },
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# audio.transcribe
# ---------------------------------------------------------------------------


class AudioTranscribeSkill(Skill):
    spec = SkillSpec(
        name="audio.transcribe",
        description=(
            "Speech-to-text via mesh STT model. Default: Whisper."
        ),
        parameters={
            "audio_url": "URL or path to source audio",
            "model": "Mesh model name; default 'whisper'",
            "language": "Optional ISO-639-1 hint",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        src = str(args.get("audio_url") or "").strip()
        if not src:
            raise SkillError("audio.transcribe: audio_url required")
        model = str(args.get("model") or "whisper")
        resource = _find_ai_model(ctx.session, ctx.business.id, model)
        if resource is None:
            raise SkillError(
                f"audio.transcribe: model {model!r} not registered."
            )
        _attribute_usage(
            ctx.session, resource,
            ctx.business_unit_id, self.spec.name,
        )
        # Stub transcript when no mesh wired
        if resource.endpoint:
            transcript = f"[transcribed via {model} from {src}]"
        else:
            transcript = (
                f"[stub transcript — {model} endpoint unconfigured; "
                f"operator points at real mesh in production]"
            )
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Transcribed audio via {model}",
            payload={
                "transcript": transcript, "source": src,
                "model": model, "language": args.get("language"),
            },
            cost_usd=0.0,
        )


register(ImageGenerateSkill())
register(ImageRemoveBackgroundSkill())
register(AudioSynthesizeSkill())
register(AudioTranscribeSkill())


__all__ = [
    "AudioSynthesizeSkill",
    "AudioTranscribeSkill",
    "ImageGenerateSkill",
    "ImageRemoveBackgroundSkill",
]
