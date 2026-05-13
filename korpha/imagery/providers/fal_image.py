"""fal.ai image-gen provider.

Pay-per-image, similar shape to Replicate. Often faster + cheaper for
FLUX models. Open-weights model selection (FLUX schnell/dev/pro,
SDXL, Recraft, Ideogram, Imagen, Stable Cascade, AuraFlow, etc.).

fal.ai API shape (synchronous endpoint, no polling needed for fast
models):

  POST https://fal.run/<model_path>
    headers: Authorization: Key <key>
    body: {"prompt": ..., "image_size": ..., "num_images": ...}
  → {"images": [{"url": "https://...png"}, ...], ...}

Slow models (FLUX pro variants) need the queue API instead — we keep
this provider simple and use the sync ``fal.run/<model>`` endpoint;
users wanting Pro can pick replicate.com or wait for queue support.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from korpha.imagery.provider import (
    ImageGenProvider,
    ImageGenRequest,
    ImageGenResult,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://fal.run"
_DEFAULT_MODEL = "fal-ai/flux/dev"
"""FLUX Dev — fast (10-20s), good quality, ~$0.025/image. Pro
variants need queue API; not yet wired."""


@dataclass
class FalImageProvider(ImageGenProvider):
    name: str = "fal"
    api_key: str = ""
    """fal.ai key. Required."""

    default_model: str = _DEFAULT_MODEL
    """fal.ai model path, e.g. ``fal-ai/flux/dev``,
    ``fal-ai/flux/schnell``, ``fal-ai/recraft-v3``,
    ``fal-ai/ideogram/v2``."""

    request_timeout_seconds: float = 120.0

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_API_BASE,
                headers={
                    "Authorization": f"Key {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.request_timeout_seconds,
            )
        return self._client

    async def generate(self, request: ImageGenRequest) -> ImageGenResult:
        if not self.api_key:
            return ImageGenResult(
                success=False, image_paths=[],
                error="fal.ai API key not configured.",
            )

        model = str(request.extra.get("model") or self.default_model)
        prompt = request.prompt
        if request.style_hint:
            prompt = f"{prompt}, {request.style_hint} style"

        # fal.ai uses ``image_size`` enums like ``square_hd``, ``landscape_4_3``
        # OR an explicit ``{width, height}`` object. We pass the explicit
        # form to honor the request.
        payload: dict[str, Any] = {
            "prompt": prompt,
            "image_size": {
                "width": request.width,
                "height": request.height,
            },
            "num_images": request.num_images,
        }
        if request.negative_prompt:
            payload["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            payload["seed"] = request.seed
        for k, v in request.extra.items():
            if k != "model":
                payload[k] = v

        client = self._get_client()
        try:
            resp = await client.post(f"/{model}", json=payload)
        except httpx.RequestError as exc:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"fal.ai request error: {exc}",
            )

        if resp.status_code >= 400:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"fal.ai {resp.status_code}: {resp.text[:300]}",
            )

        body = resp.json()
        images = body.get("images") or []
        if not images:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"fal.ai returned no images: {resp.text[:300]}",
            )

        urls: list[str] = []
        for entry in images:
            if isinstance(entry, dict):
                url = entry.get("url")
                if isinstance(url, str):
                    urls.append(url)
            elif isinstance(entry, str):
                urls.append(entry)

        paths = await self._download(urls, save_to=request.save_to)
        return ImageGenResult(
            success=True,
            image_paths=paths,
            model_used=f"fal/{model}",
            cost_usd=0.0,  # fal doesn't return per-call cost in the response
            raw={"urls": urls, "raw_response": body},
        )

    async def _download(
        self, urls: list[str], *, save_to: Path | None
    ) -> list[Path]:
        client = self._get_client()
        if save_to is not None and save_to.suffix:
            target_dir = save_to.parent
        elif save_to is not None:
            target_dir = save_to
        else:
            target_dir = Path.home() / ".korpha" / "generated_images"
        target_dir = Path(target_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)

        paths: list[Path] = []
        for i, url in enumerate(urls):
            try:
                r = await client.get(url, timeout=60.0)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("fal.ai download failed %s: %s", url, exc)
                continue
            ext = ".png"
            if url.lower().endswith((".jpg", ".jpeg")):
                ext = ".jpg"
            elif url.lower().endswith(".webp"):
                ext = ".webp"
            if save_to is not None and save_to.suffix and i == 0:
                target = save_to
            else:
                from uuid import uuid4

                target = target_dir / f"fal_{uuid4().hex[:12]}{ext}"
            target.write_bytes(r.content)
            paths.append(target)
        return paths

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["FalImageProvider"]
