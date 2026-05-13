"""Vision-capable model registry + suggestions.

What a "vision-capable" model is: one that accepts image content as
part of the chat-completion request (data URL, public URL, or base64).
We use this to (a) auto-detect whether the user's configured Pro model
already covers the Vision tier and (b) suggest a sensible default if it
doesn't.

The registry is intentionally substring-based — model IDs evolve, and
we don't want to ship a brittle exact-match list. Adding a new family
is one line.
"""
from __future__ import annotations

# Substring patterns that mark a model as vision-capable. Lowercased.
# Match logic: any pattern in this set as a substring of the model ID
# (also lowercased). Order doesn't matter; first hit wins.
_VISION_PATTERNS: tuple[str, ...] = (
    # Open-weights frontier — what we recommend. Be precise: Kimi K2
    # base is text-only; K2.6 adds vision. Match the version.
    "kimi-k2.6",
    "kimi-k3",            # forward-compat for the next Moonshot vision generation
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "llama-3.2-vision",
    "llama-3.3-vision",
    "glm-4v",
    "glm-5v",
    "pixtral",            # Mistral
    "internvl",
    "intern-vl",
    "llava",
    "minicpm-v",
    "molmo",              # Allen AI
    "nemotron-3-nano-omni",   # NVIDIA — current best open-weights vision
    "nemotron-3-vision",
    "nemotron-vision",
    "deepseek-vl",
    "phi-3-vision",
    "phi-3.5-vision",
    "phi-4-vision",
    # Closed models we *detect* but never recommend (memory:
    # feedback_open_weights_only). Listed so users with these keys
    # don't have the wizard incorrectly think they need a separate
    # vision model.
    "gpt-4o", "gpt-4-vision", "gpt-4-turbo", "gpt-5",
    "claude-3", "claude-haiku", "claude-sonnet", "claude-opus",
    "gemini",
)


def model_supports_vision(model_id: str) -> bool:
    """Best-guess: does this model accept image input?

    False is safe (we just recommend a separate vision model). False-
    positive is also recoverable — the runtime will surface a provider
    error and the user can swap. Don't optimize for purity here.
    """
    if not model_id:
        return False
    lower = model_id.lower()
    return any(pat in lower for pat in _VISION_PATTERNS)


# Recommended default vision model when the user's Pro tier doesn't
# already cover it. Open-weights, free tier on OpenRouter, also
# installable locally per memory feedback_open_weights_only.
DEFAULT_VISION_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

DEFAULT_VISION_PROVIDER_HINT = (
    "Best open-weights vision model right now: NVIDIA Nemotron 3 Nano "
    "Omni. Available free on OpenRouter as "
    "``nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free``, on NVIDIA "
    "directly at https://build.nvidia.com/nvidia/"
    "nemotron-3-nano-omni-30b-a3b-reasoning, and as a local install "
    "(~30B with A3B activations — fits on 24GB VRAM)."
)


__all__ = [
    "DEFAULT_VISION_MODEL",
    "DEFAULT_VISION_PROVIDER_HINT",
    "model_supports_vision",
]
