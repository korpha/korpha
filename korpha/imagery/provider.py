"""ImageGenProvider — pluggable image-generation backend.

Same shape as ``BrowserProvider`` and the inference Provider: ABC with
``generate(request)`` + ``close()``. Skills + the wizard talk to one of
these without caring whether the backing model lives on the user's GPU,
fal.ai, replicate.com, or behind a Codex CLI binary.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ImageGenError(RuntimeError):
    """Generic image-gen failure. Subclasses can specialize per provider."""


@dataclass(frozen=True)
class ImageGenRequest:
    prompt: str
    """What to render."""

    negative_prompt: str | None = None
    """Things to avoid (most local + open-weights models support this;
    closed APIs often don't)."""

    width: int = 1024
    height: int = 1024
    num_images: int = 1
    seed: int | None = None
    """Optional seed for reproducible outputs. None = random."""

    style_hint: str | None = None
    """e.g. ``photorealistic``, ``illustration``, ``minimal``,
    ``isometric``. Folded into the prompt by providers that don't have
    a dedicated style parameter."""

    save_to: Path | None = None
    """If set, copy the output(s) to this path (or directory). Default:
    leave files where the provider put them."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras (e.g. ``model``, ``cfg_scale``,
    ``steps``). Most callers leave this empty and let the provider
    default sensibly."""


@dataclass
class ImageGenResult:
    success: bool
    image_paths: list[Path]
    """Local paths to the generated PNG/JPG files. Empty when
    ``success=False``."""

    model_used: str | None = None
    """What the backend actually rendered with — surfaced for the cost
    + audit trail."""

    cost_usd: float = 0.0
    """Marginal cost. 0 for local + subscription-paid backends; real $
    for Replicate/fal/etc."""

    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras. Don't depend on the shape across
    providers."""


class ImageGenProvider(ABC):
    """Abstract base for any image-gen backend."""

    name: str

    @abstractmethod
    async def generate(self, request: ImageGenRequest) -> ImageGenResult:
        """Render ``request`` and return the result. Should NOT raise on
        ordinary failures (rate limit, content filter, transient
        network); return ``success=False`` with an ``error`` string so
        the caller can swap providers cleanly. Reserve raise for
        configuration / setup errors that mean retrying with the same
        provider would loop."""

    async def close(self) -> None:  # noqa: B027 — default no-op intentional
        """Tear down any persistent state (HTTP client, subprocess
        pool). Default no-op; providers override when they hold a
        client."""


__all__ = [
    "ImageGenError",
    "ImageGenProvider",
    "ImageGenRequest",
    "ImageGenResult",
]
