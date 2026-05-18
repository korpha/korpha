"""Process-wide registry of video-gen providers.

Built-in backends register at import time. Plugins call
``register_provider()`` from their ``post_install`` / module-load
hook. The CMO router (and ``video.generate`` skill) read from this
registry to pick a backend per request.

Pick strategy (rough heuristic — caller can override by name):
  1. Filter to providers whose ``available()`` is True
  2. If request has audio_url, prefer ``audio_driven`` providers
  3. If request has reference_image_url, prefer image_to_video
  4. Else pick the first text_to_video provider in registration order
"""
from __future__ import annotations

import logging
from typing import Iterable

from korpha.video.provider import VideoGenProvider, VideoGenRequest

logger = logging.getLogger(__name__)


# Insertion-order dict: built-ins register first → tried first by
# default. Plugins register later → fallbacks unless explicitly named.
_PROVIDERS: dict[str, VideoGenProvider] = {}


def register_provider(provider: VideoGenProvider) -> None:
    """Add a provider to the registry. Idempotent on name —
    re-registering with the same name replaces the existing entry."""
    name = (provider.name or "").strip()
    if not name:
        raise ValueError("provider.name must be non-empty")
    if name in _PROVIDERS:
        logger.info(
            "video.registry: replacing existing provider %r", name,
        )
    _PROVIDERS[name] = provider


def registered_providers() -> Iterable[VideoGenProvider]:
    """Iterate all providers — built-ins + plugins, registration
    order preserved."""
    return _PROVIDERS.values()


def resolve_provider(
    name: str | None = None,
    *,
    request: VideoGenRequest | None = None,
) -> VideoGenProvider | None:
    """Pick a provider. ``name`` wins outright when given (caller
    knows what they want). Otherwise heuristic pick based on
    request shape. Returns None when nothing fits."""
    if name:
        return _PROVIDERS.get(name)
    if request is None:
        # No request shape to match against — pick the first
        # available provider.
        for p in _PROVIDERS.values():
            if p.available():
                return p
        return None

    available = [p for p in _PROVIDERS.values() if p.available()]
    if not available:
        return None

    if request.audio_url:
        for p in available:
            if p.capabilities().audio_driven:
                return p
    if request.reference_image_url:
        for p in available:
            if p.capabilities().image_to_video:
                return p
    for p in available:
        if p.capabilities().text_to_video:
            return p

    # Fallback: just return something so the caller can try.
    return available[0]


def clear_registry() -> None:
    """Test-only — wipe the registry. Not exported."""
    _PROVIDERS.clear()


__all__ = [
    "register_provider",
    "registered_providers",
    "resolve_provider",
]
