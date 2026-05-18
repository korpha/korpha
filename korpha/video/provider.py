"""VideoGenProvider ABC + request/result types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class VideoGenError(RuntimeError):
    """Raised when a backend can't fulfill a request — auth, quota,
    invalid input, or upstream failure. Callers (the
    ``video.generate`` skill, the CMO router) catch this and either
    fall through to the next provider or surface as a blocker."""


@dataclass(frozen=True)
class VideoGenRequest:
    """One video-generation ask.

    Backends are free to ignore params they don't support — a
    text-to-video model can skip ``reference_image_url`` silently;
    an image-to-video model raises ``VideoGenError`` if no reference
    is provided. Use ``capabilities()`` on the provider to learn
    what's supported up-front.
    """

    prompt: str
    """Natural-language description of what the video should show."""

    duration_seconds: float | None = None
    """Target length. Backends typically clamp to a supported range
    (e.g. Pika 4s, Veo 8s, HeyGen avatar matches audio length).
    None = let the backend pick."""

    aspect_ratio: str | None = None
    """One of '16:9', '9:16', '1:1', '4:5', etc. Backends that
    don't support arbitrary ratios round to the nearest supported."""

    reference_image_url: str | None = None
    """URL of a still image to use as the first frame or visual
    reference. Image-to-video backends require this; text-to-video
    ignores it."""

    audio_url: str | None = None
    """URL to an audio track (typically narration for a talking-head
    avatar). HeyGen / D-ID / Synthesia require it; Pika / Veo /
    Runway ignore it (they generate silent video)."""

    style: str | None = None
    """Free-form style hint: 'cinematic', 'cartoon', 'documentary'.
    Backends map to their own style controls; unknown styles fall
    back to neutral."""

    seed: int | None = None
    """For reproducibility. Backends that don't support seeding
    ignore this."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Backend-specific knobs the contract doesn't expose. Use
    sparingly — anything here is implicitly non-portable."""


@dataclass(frozen=True)
class VideoGenResult:
    """What a backend returns on success."""

    video_url: str | None = None
    """Public/temporary URL for the rendered video. One of video_url
    or video_path must be set."""

    video_path: str | None = None
    """Local filesystem path (for backends that render on-disk like
    HyperFrames). One of video_url or video_path must be set."""

    duration_seconds: float | None = None
    """Actual duration of the rendered output."""

    cost_usd: Decimal = Decimal("0")
    """Per-call cost. 0 for subscription / free / local backends;
    real $ for FAL / Replicate / Pika / Runway / Veo."""

    backend_name: str = ""
    """Human-readable backend name for the activity log."""

    raw: dict[str, Any] = field(default_factory=dict)
    """Raw response from the upstream API — for debug + caller
    inspection. Don't depend on its shape across backends."""


@dataclass(frozen=True)
class VideoGenCapabilities:
    """What a backend can actually do — used by the dispatcher to
    pick a provider for a given request shape."""

    text_to_video: bool = False
    image_to_video: bool = False
    audio_driven: bool = False
    """True for talking-head avatar backends (HeyGen, D-ID,
    Synthesia) that need a reference audio track."""

    max_duration_seconds: float | None = None
    """Hard ceiling. None = no documented limit."""

    aspect_ratios: tuple[str, ...] = ()
    """Supported aspect ratios. Empty tuple = backend picks freely."""

    has_watermark: bool = False
    """True for free tiers that add a watermark to output."""


class VideoGenProvider(ABC):
    """Abstract base for any video-generation backend.

    Subclass + register via ``korpha.video.registry.register_provider``.
    Each instance is stateless — the registry caches the instance, so
    instance attributes should be cheap to construct.
    """

    name: str
    """Short identifier — 'hyperframes', 'heygen', 'replicate-pika'."""

    @abstractmethod
    def capabilities(self) -> VideoGenCapabilities:
        """Describe what this backend supports. Read once at
        registration + cached by the dispatcher."""

    @abstractmethod
    async def generate(self, request: VideoGenRequest) -> VideoGenResult:
        """Execute the request. Raise :class:`VideoGenError` on
        any failure the caller should treat as 'try another
        backend'. Raise :class:`Exception` for genuine bugs."""

    def available(self) -> bool:
        """True iff this backend can actually be called right now —
        has credentials, binary on PATH, etc. Defaults to True;
        override when credentials matter."""
        return True


__all__ = [
    "VideoGenCapabilities",
    "VideoGenError",
    "VideoGenProvider",
    "VideoGenRequest",
    "VideoGenResult",
]
