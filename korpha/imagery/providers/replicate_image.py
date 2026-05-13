"""Replicate.com image-gen provider.

Pay-per-image. Open-weights model selection: FLUX (.1 / .1-pro / .1.1-
pro), SDXL, Stable Diffusion 3.5, Recraft v3, Playground v3, Imagen 4
(closed but available), HiDream, Kandinsky 3, Wuerstchen, and many
more. Versions evolve fast — we pin via the model+version slug the
user picks.

Replicate API shape:

  POST https://api.replicate.com/v1/predictions
    headers: Authorization: Bearer <token>
    body: {"version": "<sha>" or "model": "owner/name",
           "input": {"prompt": ..., ...}}
  → {"id": "<id>", "status": "starting", ...}

  GET https://api.replicate.com/v1/predictions/<id>
  → {"status": "succeeded", "output": ["https://...png"], ...}

We poll status until succeeded/failed, then download the URLs.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from korpha.imagery.provider import (
    ImageGenProvider,
    ImageGenRequest,
    ImageGenResult,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://api.replicate.com/v1"
_DEFAULT_MODEL = "black-forest-labs/flux-1.1-pro"
"""FLUX.1.1 Pro — Replicate's go-to default for open-weights image gen
in 2026. Solid quality, fast, ~$0.04/image."""


@dataclass
class ReplicateImageProvider(ImageGenProvider):
    name: str = "replicate"
    api_token: str = ""
    """Replicate API token. Required."""

    default_model: str = _DEFAULT_MODEL
    """Owner/name of the model to use when ``request.extra['model']``
    is unset. Override per-call via ``extra``."""

    poll_interval_seconds: float = 2.0
    """How often to poll the prediction-status endpoint."""

    request_timeout_seconds: float = 180.0

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_API_BASE,
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def generate(self, request: ImageGenRequest) -> ImageGenResult:
        if not self.api_token:
            return ImageGenResult(
                success=False, image_paths=[],
                error="Replicate API token not configured.",
            )

        model = str(request.extra.get("model") or self.default_model)
        prompt = request.prompt
        if request.style_hint:
            prompt = f"{prompt}, {request.style_hint} style"

        # Replicate's `input` shape varies per model. We pass a sane
        # baseline that most image-gen models accept.
        input_payload: dict[str, object] = {
            "prompt": prompt,
            "width": request.width,
            "height": request.height,
            "num_outputs": request.num_images,
        }
        if request.negative_prompt:
            input_payload["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            input_payload["seed"] = request.seed
        for k, v in request.extra.items():
            if k != "model":
                input_payload[k] = v

        client = self._get_client()
        try:
            resp = await client.post(
                f"/models/{model}/predictions",
                json={"input": input_payload},
            )
        except httpx.RequestError as exc:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"Replicate request error: {exc}",
            )

        if resp.status_code >= 400:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"Replicate {resp.status_code}: {resp.text[:300]}",
            )

        prediction = resp.json()
        prediction_id = prediction.get("id")
        if not prediction_id:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"No prediction id in response: {resp.text[:300]}",
            )

        # Poll until done. Replicate also supports webhooks; for OSS
        # one-Founder use polling is simpler.
        deadline = (
            asyncio.get_running_loop().time() + self.request_timeout_seconds
        )
        status = prediction.get("status")
        output: list[str] | None = None
        while True:
            if status in ("succeeded", "failed", "canceled"):
                output = prediction.get("output")
                break
            if asyncio.get_running_loop().time() > deadline:
                return ImageGenResult(
                    success=False, image_paths=[],
                    error=f"Replicate poll timeout after {self.request_timeout_seconds}s",
                )
            await asyncio.sleep(self.poll_interval_seconds)
            try:
                poll = await client.get(f"/predictions/{prediction_id}")
            except httpx.RequestError as exc:
                return ImageGenResult(
                    success=False, image_paths=[],
                    error=f"Replicate poll error: {exc}",
                )
            if poll.status_code >= 400:
                return ImageGenResult(
                    success=False, image_paths=[],
                    error=f"Replicate poll {poll.status_code}: {poll.text[:300]}",
                )
            prediction = poll.json()
            status = prediction.get("status")

        if status != "succeeded" or not output:
            err = prediction.get("error") or f"status={status}"
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"Replicate generation failed: {err}",
            )

        # Download to local disk. Replicate output is either a single
        # URL or a list depending on model — normalise to list.
        urls = output if isinstance(output, list) else [output]
        paths = await self._download(urls, save_to=request.save_to)
        return ImageGenResult(
            success=True,
            image_paths=paths,
            model_used=f"replicate/{model}",
            cost_usd=0.0,  # Replicate doesn't expose per-call cost; user
                          # tracks via dashboard.
            raw={"prediction_id": prediction_id, "urls": urls},
        )

    async def _download(
        self, urls: list[str], *, save_to: Path | None
    ) -> list[Path]:
        """Pull each output URL down to local disk."""
        client = self._get_client()
        if save_to is not None:
            target_dir = (
                save_to
                if save_to.suffix == ""
                else save_to.parent
            )
            target_dir = Path(target_dir).expanduser()
            target_dir.mkdir(parents=True, exist_ok=True)
        else:
            target_dir = Path.home() / ".korpha" / "generated_images"
            target_dir.mkdir(parents=True, exist_ok=True)

        paths: list[Path] = []
        for i, url in enumerate(urls):
            try:
                r = await client.get(url, timeout=60.0)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("replicate download failed %s: %s", url, exc)
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

                target = target_dir / f"replicate_{uuid4().hex[:12]}{ext}"
            target.write_bytes(r.content)
            paths.append(target)
        return paths

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["ReplicateImageProvider"]
