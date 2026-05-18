"""Built-in adapters that bridge the VideoGenProvider contract to
existing AIgenteur skills.

We don't duplicate render logic — these adapters call the same
skill instances the agent uses directly. Plugins that add new
backends (Pika, Veo, Runway, etc.) implement VideoGenProvider from
scratch.
"""
from __future__ import annotations

import logging
import os
import shutil
from decimal import Decimal

from korpha.video.provider import (
    VideoGenCapabilities,
    VideoGenError,
    VideoGenProvider,
    VideoGenRequest,
    VideoGenResult,
)

logger = logging.getLogger(__name__)


class HyperFramesBackend(VideoGenProvider):
    """Local ffmpeg-based composition via :mod:`korpha.skills.creative`.
    Cheap (free), runs on Mike's laptop, only does composition (no
    actual text→video generation). Useful for stitching avatar
    talking-heads with title cards, b-roll, transitions."""

    name = "hyperframes"

    def capabilities(self) -> VideoGenCapabilities:
        return VideoGenCapabilities(
            text_to_video=False,
            image_to_video=True,  # uses still images as keyframes
            audio_driven=False,
            max_duration_seconds=None,
            aspect_ratios=("16:9", "9:16", "1:1", "4:5"),
            has_watermark=False,
        )

    def available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    async def generate(self, request: VideoGenRequest) -> VideoGenResult:
        # The actual rendering still goes through the skill —
        # the contract is just a routing layer for the plugin system.
        if not request.reference_image_url and not request.extra.get(
            "frames",
        ):
            raise VideoGenError(
                "hyperframes needs reference frames; supply "
                "reference_image_url or extra.frames",
            )
        # Delegate to the skill. We don't import it eagerly because
        # the creative module may be heavy + this provider is
        # often unused.
        try:
            from korpha.skills import default_registry
        except ImportError as exc:
            raise VideoGenError(f"skills layer unavailable: {exc}") from exc
        skill = default_registry.skills.get("creative.hyperframes")
        if skill is None:
            raise VideoGenError(
                "creative.hyperframes skill not registered",
            )
        raise VideoGenError(
            "hyperframes adapter requires a SkillContext — call the "
            "creative.hyperframes skill directly, not via the "
            "VideoGenProvider contract. The provider contract only "
            "advertises capabilities for now.",
        )


class HeyGenBackend(VideoGenProvider):
    """Talking-head avatar via HeyGen API. Audio-driven (requires
    an audio_url). Paid per-second; HeyGen subscription covers
    most personal-brand workloads."""

    name = "heygen"

    def capabilities(self) -> VideoGenCapabilities:
        return VideoGenCapabilities(
            text_to_video=False,
            image_to_video=False,
            audio_driven=True,
            max_duration_seconds=300.0,
            aspect_ratios=("16:9", "9:16", "1:1"),
            has_watermark=False,
        )

    def available(self) -> bool:
        return bool(os.environ.get("HEYGEN_API_KEY"))

    async def generate(self, request: VideoGenRequest) -> VideoGenResult:
        if not request.audio_url:
            raise VideoGenError(
                "heygen needs an audio_url (talking-head avatar)",
            )
        raise VideoGenError(
            "heygen adapter requires a SkillContext — call the "
            "creative.heygen_avatar skill directly, not via the "
            "VideoGenProvider contract. The provider contract only "
            "advertises capabilities for now.",
        )


def register_builtin_video_providers() -> None:
    """Register the two built-in adapters. Called once at module load
    from korpha.skills.__init__ so the registry is populated by the
    time the CMO router needs it."""
    from korpha.video.registry import register_provider

    register_provider(HyperFramesBackend())
    register_provider(HeyGenBackend())


__all__ = [
    "HeyGenBackend",
    "HyperFramesBackend",
    "register_builtin_video_providers",
]
