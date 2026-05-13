"""Image generation: provider abstraction + multiple backends.

Image gen is its own concern, separate from:
- Inference (text/vision LLM calls — see korpha/inference/)
- Image *understanding* / vision (analyzing screenshots — Vision tier
  on the inference side, see korpha/inference/vision.py)

Backends today:
- ``CodexCLIImageProvider`` — uses an installed ``codex`` CLI's
  built-in imagegen (subscription-paid via ChatGPT Plus/Pro/Max). The
  best image model right now, but only if the user has Codex.
- ``ReplicateImageProvider`` — replicate.com HTTP API. Pay-per-image,
  open-weights model selection (FLUX, SDXL, Recraft, Playground v3,
  Imagen 4 via OpenRouter, etc.).
- ``FalImageProvider`` — fal.ai HTTP API. Same shape as Replicate;
  often faster and cheaper for FLUX.
- ``LocalSDProvider`` — talks to any A1111-compatible HTTP endpoint
  (Automatic1111, Forge, ComfyUI via the AUTOMATIC1111 plugin,
  sd.cpp's WebUI). Lets the Founder run their own image model on
  their own GPU (or LAN). $0 marginal cost.

Hardcoding Codex was a real bug — most Founders won't have it. The
abstraction here lets people pick whatever they have access to.
"""
from korpha.imagery.provider import (
    ImageGenError,
    ImageGenProvider,
    ImageGenRequest,
    ImageGenResult,
)
from korpha.imagery.providers.codex_cli_image import CodexCLIImageProvider
from korpha.imagery.providers.fal_image import FalImageProvider
from korpha.imagery.providers.local_sd import LocalSDProvider
from korpha.imagery.providers.replicate_image import ReplicateImageProvider

__all__ = [
    "CodexCLIImageProvider",
    "FalImageProvider",
    "ImageGenError",
    "ImageGenProvider",
    "ImageGenRequest",
    "ImageGenResult",
    "LocalSDProvider",
    "ReplicateImageProvider",
]
