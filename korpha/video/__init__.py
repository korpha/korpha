"""Video generation plugin contract.

Lets the community ship video-generation backends without forking
the core agent. The contract is intentionally narrow — every backend
takes a text prompt + optional reference image / start image / aspect
ratio / duration, and returns a URL or local file path to the rendered
video.

Built-in backends:
  * :class:`HyperFramesBackend` — ffmpeg-based local composition
    (already in :mod:`korpha.skills.creative`).
  * :class:`HeyGenBackend` — talking-head avatar via HeyGen API
    (already in :mod:`korpha.skills.creative`).

Both are *adapters* over existing skills, so the plugin contract
doesn't duplicate logic. Plugins that want to add Replicate, Pika,
Runway, FAL Veo, Kling, etc. implement ``VideoGenProvider`` and
register via the plugin system.

Mirrors Hermes PR #25126 — we take their ABC + registry shape but
ship our own adapter set (no Grok-Imagine / FAL backends; those are
the founder's call to wire up if needed).
"""
from korpha.video.provider import (
    VideoGenCapabilities,
    VideoGenError,
    VideoGenProvider,
    VideoGenRequest,
    VideoGenResult,
)
from korpha.video.registry import (
    register_provider,
    registered_providers,
    resolve_provider,
)

__all__ = [
    "VideoGenCapabilities",
    "VideoGenError",
    "VideoGenProvider",
    "VideoGenRequest",
    "VideoGenResult",
    "register_provider",
    "registered_providers",
    "resolve_provider",
]
