"""ImageGenService — picks a backend from providers.yaml.

Schema (under ``image_providers:`` top-level key in ``providers.yaml``):

```yaml
image_providers:
  - preset: replicate
    api_key_env: REPLICATE_API_TOKEN
    default_model: black-forest-labs/flux-1.1-pro

  - preset: fal
    api_key_env: FAL_KEY
    default_model: fal-ai/flux/dev

  - preset: local-sd
    base_url: http://localhost:7860
    default_model: sdxl_base_1.0  # optional

  - preset: codex-cli
    # No config — uses installed `codex` binary's auth
```

Order matters — the first entry is the default. ``korpha config
image-add`` writes to this list.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from korpha.imagery.provider import (
    ImageGenProvider,
    ImageGenRequest,
    ImageGenResult,
)
from korpha.imagery.providers.codex_cli_image import CodexCLIImageProvider
from korpha.imagery.providers.fal_image import FalImageProvider
from korpha.imagery.providers.local_sd import LocalSDProvider
from korpha.imagery.providers.replicate_image import ReplicateImageProvider

IMAGE_PRESETS: tuple[str, ...] = ("replicate", "fal", "local-sd", "codex-cli")


class ImageConfigError(ValueError):
    """Raised when image_providers section is malformed."""


@dataclass
class ImageGenService:
    """Skill-facing facade. Tries providers in order; first success wins."""

    providers: list[ImageGenProvider]

    async def generate(self, request: ImageGenRequest) -> ImageGenResult:
        if not self.providers:
            return ImageGenResult(
                success=False, image_paths=[],
                error=(
                    "No image provider configured. Run `korpha config "
                    "image-add` to add one (Replicate / fal.ai / local SD "
                    "WebUI / Codex CLI)."
                ),
            )
        last_err: str | None = None
        for p in self.providers:
            result = await p.generate(request)
            if result.success:
                return result
            last_err = result.error or f"{p.name}: unknown failure"
        return ImageGenResult(
            success=False, image_paths=[],
            error=last_err or "all image providers failed",
        )

    async def close(self) -> None:
        import contextlib

        for p in self.providers:
            with contextlib.suppress(Exception):
                await p.close()


def build_provider(entry: dict[str, Any]) -> ImageGenProvider:
    """Construct one provider from a parsed providers.yaml entry."""
    if not isinstance(entry, dict):
        raise ImageConfigError(f"image_providers entry must be a mapping, got {type(entry).__name__}")
    preset = entry.get("preset")
    if preset not in IMAGE_PRESETS:
        raise ImageConfigError(
            f"unknown image preset {preset!r}. Known: {', '.join(IMAGE_PRESETS)}"
        )

    if preset == "replicate":
        token = _resolve_secret(entry, "api_key", "api_key_env")
        if not token:
            raise ImageConfigError("replicate preset needs api_key or api_key_env")
        return ReplicateImageProvider(
            api_token=token,
            default_model=str(entry.get("default_model") or "black-forest-labs/flux-1.1-pro"),
        )
    if preset == "fal":
        key = _resolve_secret(entry, "api_key", "api_key_env")
        if not key:
            raise ImageConfigError("fal preset needs api_key or api_key_env")
        return FalImageProvider(
            api_key=key,
            default_model=str(entry.get("default_model") or "fal-ai/flux/dev"),
        )
    if preset == "local-sd":
        base = str(entry.get("base_url") or "http://localhost:7860")
        api_key = _resolve_secret(entry, "api_key", "api_key_env")
        return LocalSDProvider(
            base_url=base,
            api_key=api_key,
            default_model=entry.get("default_model"),
        )
    # codex-cli: nothing to configure
    return CodexCLIImageProvider()


def _resolve_secret(entry: dict[str, Any], inline_key: str, env_key: str) -> str | None:
    inline = entry.get(inline_key)
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    env_name = entry.get(env_key)
    if isinstance(env_name, str) and env_name.strip():
        return os.getenv(env_name.strip()) or None
    return None


def load_image_providers(path: Path | None = None) -> list[ImageGenProvider]:
    """Read ``image_providers:`` from providers.yaml; return built
    providers in declared order. Missing file or section → fall back to
    ``CodexCLIImageProvider`` if the ``codex`` binary is on PATH (it's
    the configured default for users who logged in to ChatGPT/Codex);
    otherwise empty list.

    The fallback prevents the "AI tool selection and budget" hallucination
    pattern: when no provider was wired but Codex IS available, the agent
    asks the founder which image tool to pick. With the fallback in place,
    the system has a real default and the LLM sees it in the capabilities
    preamble (see korpha/cofounder/capabilities.py).
    """
    import shutil

    import yaml

    from korpha.inference.config import config_path

    p = path or config_path()
    entries: list = []
    if p.exists():
        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw = body.get("image_providers") or []
        if raw and not isinstance(raw, list):
            raise ImageConfigError("image_providers must be a list")
        entries = list(raw)

    out: list[ImageGenProvider] = []
    for entry in entries:
        try:
            out.append(build_provider(entry))
        except ImageConfigError:
            # Skip a bad entry rather than blowing up the whole load —
            # surface via the wizard's --validate path later.
            continue

    if not out and shutil.which("codex") is not None:
        # Implicit default: a logged-in Codex CLI is the strongest
        # image-gen signal we can detect. Pre-wire gpt-image-2 so the
        # team has something to call without making the founder edit
        # providers.yaml.
        out.append(CodexCLIImageProvider())

    return out


__all__ = [
    "IMAGE_PRESETS",
    "ImageConfigError",
    "ImageGenService",
    "build_provider",
    "load_image_providers",
]
