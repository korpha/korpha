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


class GrokImagineBackend(VideoGenProvider):
    """Text-to-video via xAI's Grok Imagine. Uses the user's existing
    SuperGrok subscription via xai_oauth — no per-call billing, no
    API key needed beyond what ``korpha auth add xai-oauth`` writes.

    Pairs naturally with the X Search skill: founders who already
    subscribed to X Premium+ to use Grok get video generation for
    free as part of the same subscription.
    """

    name = "grok-imagine"

    # Endpoint name based on xAI's OpenAI-compatibility surface
    # (https://api.x.ai/v1/...). The Grok Imagine endpoint URL +
    # model id may change as xAI publishes the official spec — if the
    # API shape shifts, only this class needs updating.
    _ENDPOINT_URL = "https://api.x.ai/v1/videos/generations"
    _DEFAULT_MODEL = "grok-imagine"

    def capabilities(self) -> VideoGenCapabilities:
        return VideoGenCapabilities(
            text_to_video=True,
            image_to_video=True,  # accepts reference_image_url
            audio_driven=False,
            max_duration_seconds=10.0,  # typical Grok Imagine clip
            aspect_ratios=("16:9", "9:16", "1:1"),
            has_watermark=False,
        )

    def available(self) -> bool:
        """True iff the user has completed `korpha auth add xai-oauth`
        and the token vault has a non-expired access token."""
        try:
            from korpha.inference.xai_oauth import is_configured
            return is_configured()
        except Exception:
            return False

    async def generate(self, request: VideoGenRequest) -> VideoGenResult:
        """POST to xAI's video-gen endpoint with the OAuth bearer.

        Returns the rendered video URL. If the subscription doesn't
        include Grok Imagine, raises VideoGenError with the upstream
        message so the caller can show a clear upgrade hint.
        """
        if not self.available():
            raise VideoGenError(
                "Grok Imagine needs SuperGrok auth — run `korpha "
                "auth add xai-oauth` first (uses your X Premium+ "
                "subscription, no API key)."
            )
        try:
            from korpha.inference.xai_oauth import get_auth
            auth = get_auth()
        except Exception as exc:
            raise VideoGenError(
                f"Couldn't read xAI OAuth state: {exc}"
            ) from exc

        import httpx
        payload: dict = {
            "model": self._DEFAULT_MODEL,
            "prompt": request.prompt,
        }
        if request.duration_seconds:
            payload["duration_seconds"] = float(request.duration_seconds)
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.reference_image_url:
            payload["reference_image_url"] = request.reference_image_url
        if request.seed is not None:
            payload["seed"] = int(request.seed)
        if request.extra:
            for k, v in request.extra.items():
                payload.setdefault(k, v)

        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    self._ENDPOINT_URL, json=payload, headers=headers,
                )
            if resp.status_code >= 400:
                raise VideoGenError(
                    f"Grok Imagine {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            data = resp.json()
        except httpx.HTTPError as exc:
            raise VideoGenError(f"Grok Imagine HTTP error: {exc}") from exc

        # Two likely shapes (OpenAI-compat + xAI-specific):
        #   {"data": [{"url": "https://..."}]}
        #   {"url": "https://..."}
        video_url: str | None = None
        if isinstance(data.get("data"), list) and data["data"]:
            video_url = data["data"][0].get("url")
        if not video_url:
            video_url = data.get("url") or data.get("video_url")
        if not video_url:
            raise VideoGenError(
                f"Grok Imagine returned no video URL in response: "
                f"{str(data)[:300]}"
            )

        return VideoGenResult(
            video_url=video_url,
            duration_seconds=request.duration_seconds,
            cost_usd=Decimal("0"),  # subscription-billed
            backend_name="grok-imagine",
            raw=data,
        )


def register_builtin_video_providers() -> None:
    """Register the built-in adapters. Called once at module load
    from korpha.skills.__init__ so the registry is populated by the
    time the CMO router needs it."""
    from korpha.video.registry import register_provider

    register_provider(HyperFramesBackend())
    register_provider(HeyGenBackend())
    register_provider(GrokImagineBackend())


__all__ = [
    "HeyGenBackend",
    "HyperFramesBackend",
    "register_builtin_video_providers",
]
