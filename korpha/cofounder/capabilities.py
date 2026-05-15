"""Available-capabilities preamble for Director / Worker system prompts.

Problem this fixes: agents don't know what's installed and configured.
When the CTO hits "I need image generation" during card execution, it has
no idea that ``imagery.generate_image`` already exists with
``codex/gpt-image-2`` configured as the default. So it invents the
question — "should we use DALL·E or Midjourney or Stable Diffusion and how
much should we budget?" — and surfaces a blocker to the founder.

The founder then sees an approval like "AI tool selection and budget" with
options the system already has a policy for. Wasted turn for them, and
the team looks indecisive.

This module renders a short "Available capabilities" section that gets
injected into every Director.attempt and Worker.attempt system prompt.
The LLM sees the configured tooling + defaults inline and stops asking.

Keep this preamble **short** — every Director call pays the prompt tax.
Aim for <800 chars; list only capabilities likely to come up in card
execution (image gen, video, voice, search, credentials).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _image_capability_line() -> str:
    """Describe what image generation is available, or signal it's
    unconfigured so the agent stops inventing options."""
    try:
        from korpha.imagery.service import load_image_providers
        providers = load_image_providers()
    except Exception:  # noqa: BLE001
        providers = []
    if not providers:
        # Don't lie — tell the agent it's unconfigured. The skill still
        # exists; the founder needs to add an entry to providers.yaml.
        # Crucially, the agent should NOT ask the founder to pick a tool;
        # it should call `credentials.set` or recommend `aigenteur setup
        # image-provider` if surfacing the gap.
        return (
            "- Image generation: imagery.generate_image (skill exists, "
            "no provider configured yet — recommend "
            "`aigenteur setup image-provider` or skip image work)"
        )
    # Describe what IS configured. First provider wins as default.
    primary = providers[0]
    kind = type(primary).__name__
    default = getattr(primary, "default_model", None)
    label_map = {
        "CodexCLIImageProvider": ("codex CLI", "gpt-image-2"),
        "ReplicateImageProvider": ("replicate", default or "flux-1.1-pro"),
        "FalImageProvider": ("fal.ai", default or "fal-ai/flux/dev"),
        "LocalSDProvider": ("local SD WebUI", default or "auto"),
    }
    label, model = label_map.get(
        kind, (kind, default or "default"),
    )
    return (
        f"- Image generation: imagery.generate_image "
        f"(configured: {label}, model={model}). "
        "Call it directly — do NOT ask the founder to pick a tool."
    )


def _voice_capability_line() -> str:
    """Voice / TTS provider configured?"""
    try:
        from korpha.voice.service import load_voice_providers
        providers = load_voice_providers()
    except Exception:  # noqa: BLE001
        providers = []
    if not providers:
        return ""
    kind = type(providers[0]).__name__
    return (
        f"- Voice / TTS: voice.synthesize (configured: {kind}). "
        "Call directly when you need narration."
    )


def _video_capability_line() -> str:
    """Video composition skill is always available locally
    (creative.hyperframes), so it's worth listing unconditionally."""
    return (
        "- Video composition: creative.hyperframes (local ffmpeg). "
        "Use it to stitch images into a video. "
        "Avatar talking-head: creative.heygen_avatar (needs HeyGen API)."
    )


def _search_capability_line() -> str:
    """Web search / scrape via web.* skills, if configured."""
    # web search providers are gated behind env vars; just point at the
    # umbrella skill so the agent calls it rather than asks "should we
    # use Google or Brave or DDG?"
    return (
        "- Web search: web.search (uses configured provider — Brave / "
        "DDG / Exa / Tavily). Call directly when you need fresh info."
    )


def build_capabilities_preamble() -> str:
    """Render the full preamble. Empty string if nothing useful to say
    (extreme edge case — image line always returns something)."""
    lines = [
        _image_capability_line(),
        _video_capability_line(),
        _search_capability_line(),
    ]
    voice = _voice_capability_line()
    if voice:
        lines.append(voice)
    body = "\n".join(line for line in lines if line)
    return (
        "Available capabilities (already configured — use directly, "
        "do NOT ask the Founder to pick a tool):\n"
        + body
    )


__all__ = ["build_capabilities_preamble"]
