"""Codex Responses API image-gen provider — gpt-image-2 via OAuth.

Phase 2.3 of the Codex transport port. Replaces the
:class:`CodexCLIImageProvider` subprocess approach with a direct call
to ``chatgpt.com/backend-api/codex/responses`` using the
``image_generation`` tool. Saves the base64 PNG that Codex returns to
``$KORPHA_HOME/cache/images/``.

Maps directly to Hermes's ``plugins/image_gen/openai-codex/__init__.py``
but adapted to our :class:`ImageGenProvider` interface.

Three quality tiers (mirrors Hermes — request via ``extra={"quality":
"low|medium|high"}`` on :class:`ImageGenRequest`):

  - ``gpt-image-2-low``    — ~15s, fastest, lowest cost
  - ``gpt-image-2-medium`` — ~40s, balanced (default)
  - ``gpt-image-2-high``   — ~2min, highest fidelity

Aspect ratios: ``landscape`` (1536×1024), ``square`` (1024×1024),
``portrait`` (1024×1536). Selected from ``request.width`` /
``request.height`` (closest match wins).
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from korpha.imagery.provider import (
    ImageGenProvider,
    ImageGenRequest,
    ImageGenResult,
)
from korpha.inference.codex_oauth import (
    CodexAuthError,
    cloudflare_headers,
    get_codex_auth,
    is_configured as codex_is_configured,
)

logger = logging.getLogger(__name__)

_BASE = "https://chatgpt.com/backend-api/codex"

# The Codex Responses surface uses ``gpt-5.4`` as the *chat* host that
# calls the ``image_generation`` tool. The tool itself renders with
# ``gpt-image-2`` at the requested quality.
_CHAT_MODEL = "gpt-5.4"
_API_MODEL = "gpt-image-2"

_QUALITY_TIERS = {
    "low": ("low", "~15s"),
    "medium": ("medium", "~40s"),
    "high": ("high", "~2min"),
}


def _resolve_size(width: int, height: int) -> str:
    """Pick the closest Codex-supported size for the request's aspect."""
    if width > height:
        return "1536x1024"  # landscape
    if height > width:
        return "1024x1536"  # portrait
    return "1024x1024"      # square


def _cache_dir() -> Path:
    """Where to drop generated PNGs. Defaults under ``$KORPHA_HOME``
    (or ``~/.korpha``)."""
    import os
    base = os.environ.get("KORPHA_HOME") or str(Path.home() / ".korpha")
    out = Path(base) / "cache" / "images"
    out.mkdir(parents=True, exist_ok=True)
    return out


@dataclass
class CodexResponsesImageProvider(ImageGenProvider):
    """gpt-image-2 via Codex OAuth, no separate OPENAI_API_KEY needed."""

    name: str = "codex-responses-image"

    async def generate(self, request: ImageGenRequest) -> ImageGenResult:
        if not codex_is_configured():
            return ImageGenResult(
                success=False, image_paths=[],
                error=(
                    "Codex OAuth not available. Run `codex login` "
                    "or pick a different image-gen provider in "
                    "providers.yaml under image_providers:"
                ),
            )
        try:
            auth = get_codex_auth()
        except CodexAuthError as exc:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"codex auth: {exc}",
            )

        prompt = (request.prompt or "").strip()
        if request.style_hint:
            prompt = f"{prompt}\n\nStyle: {request.style_hint}"
        if request.negative_prompt:
            prompt = f"{prompt}\n\nAvoid: {request.negative_prompt}"

        quality = str(request.extra.get("quality", "medium")).lower()
        if quality not in _QUALITY_TIERS:
            quality = "medium"
        size = _resolve_size(request.width, request.height)

        instructions = (
            "You are an assistant that must fulfill image generation "
            "requests by calling the image_generation tool. Don't "
            "answer in text — just call the tool with the prompt."
        )
        payload = {
            "model": _CHAT_MODEL,
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }],
            "tools": [{
                "type": "image_generation",
                "model": _API_MODEL,
                "size": size,
                "quality": _QUALITY_TIERS[quality][0],
                "output_format": "png",
                "background": "opaque",
                "partial_images": 1,
            }],
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "required",
                "tools": [{"type": "image_generation"}],
            },
        }
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **cloudflare_headers(auth.access_token),
        }

        image_b64: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=20.0),
            ) as client:
                async with client.stream(
                    "POST", f"{_BASE}/responses",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        return ImageGenResult(
                            success=False, image_paths=[],
                            error=(
                                f"Codex Responses {resp.status_code}: "
                                + body.decode("utf-8", errors="replace")[:400]
                            ),
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        t = event.get("type", "")
                        # Each partial frame overwrites the previous; the
                        # final image arrives in output_item.done with
                        # item.type=image_generation_call and item.result
                        # = base64 PNG.
                        if t == "response.image_generation_call.partial_image":
                            partial = event.get("partial_image_b64")
                            if isinstance(partial, str) and partial:
                                image_b64 = partial
                        elif t == "response.output_item.done":
                            item = event.get("item") or {}
                            if item.get("type") == "image_generation_call":
                                result = item.get("result")
                                if isinstance(result, str) and result:
                                    image_b64 = result
        except httpx.HTTPError as exc:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"transport error: {type(exc).__name__}: {exc}",
            )

        if not image_b64:
            return ImageGenResult(
                success=False, image_paths=[],
                error="Codex Responses returned no image data",
            )

        # Decode + save the PNG.
        try:
            png_bytes = base64.b64decode(image_b64)
        except Exception as exc:  # noqa: BLE001
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"base64 decode: {type(exc).__name__}: {exc}",
            )

        cache = _cache_dir()
        fname = f"codex_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        out_path = cache / fname
        try:
            out_path.write_bytes(png_bytes)
        except OSError as exc:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"write {out_path}: {exc}",
            )

        # Honor save_to: if the caller pointed at a file, copy; if a
        # directory, copy in.
        final_paths: list[Path] = [out_path]
        if request.save_to is not None:
            import shutil
            dest = Path(request.save_to).expanduser()
            if dest.suffix:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(out_path, dest)
                final_paths = [dest]
            else:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / fname
                shutil.copy2(out_path, target)
                final_paths = [target]

        return ImageGenResult(
            success=True,
            image_paths=final_paths,
            model_used=f"codex/{_API_MODEL}-{quality}",
            cost_usd=0.0,  # subscription-paid
            raw={"size": size, "quality": quality},
        )


__all__ = ["CodexResponsesImageProvider"]
