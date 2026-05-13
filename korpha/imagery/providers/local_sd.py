"""Local image-gen via A1111-compatible HTTP endpoint.

Talks to the Stable-Diffusion-WebUI (Automatic1111) ``/sdapi/v1/txt2img``
endpoint — also exposed by Forge, ComfyUI's a1111 plugin, and sd.cpp's
``stable-diffusion.cpp --api`` server. $0 marginal cost; runs entirely
on the user's GPU (or LAN-shared GPU box).

Default base URL ``http://localhost:7860`` is the AUTOMATIC1111 default.
For ComfyUI native, point this at a small bridge or use the ComfyUI
plugin that exposes the a1111-compat API.

Returns base64 PNGs in the response — we decode + write to disk.
"""
from __future__ import annotations

import base64
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


@dataclass
class LocalSDProvider(ImageGenProvider):
    name: str = "local-sd"
    base_url: str = "http://localhost:7860"
    """A1111-compatible WebUI endpoint. Default is AUTOMATIC1111's
    standard. Point at LAN address (``http://10.0.0.5:7860``) for a
    shared GPU box."""

    api_key: str | None = None
    """Optional ``--api-auth`` token if the WebUI was started with
    authentication enabled. Most home installs have none."""

    default_model: str | None = None
    """Optional sd_model_checkpoint override. None = use whatever the
    WebUI currently has loaded. Set explicitly for SDXL / Flux merges."""

    default_steps: int = 20
    default_cfg: float = 7.0
    request_timeout_seconds: float = 240.0

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Basic {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                headers=headers,
                timeout=self.request_timeout_seconds,
            )
        return self._client

    async def generate(self, request: ImageGenRequest) -> ImageGenResult:
        prompt = request.prompt
        if request.style_hint:
            prompt = f"{prompt}, {request.style_hint}"

        payload: dict[str, Any] = {
            "prompt": prompt,
            "negative_prompt": request.negative_prompt or "",
            "width": request.width,
            "height": request.height,
            "batch_size": request.num_images,
            "n_iter": 1,
            "steps": int(request.extra.get("steps", self.default_steps)),
            "cfg_scale": float(request.extra.get("cfg_scale", self.default_cfg)),
            "sampler_name": str(request.extra.get("sampler_name", "Euler")),
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        if self.default_model and "sd_model_checkpoint" not in request.extra:
            payload["override_settings"] = {
                "sd_model_checkpoint": self.default_model,
            }
        # Allow request.extra to override any field we just set.
        for k, v in request.extra.items():
            if k not in ("model",):
                payload[k] = v

        client = self._get_client()
        try:
            resp = await client.post("/sdapi/v1/txt2img", json=payload)
        except httpx.RequestError as exc:
            return ImageGenResult(
                success=False, image_paths=[],
                error=(
                    f"Local SD WebUI request failed: {exc}. Is the WebUI "
                    f"running at {self.base_url}? Start it with --api flag."
                ),
            )

        if resp.status_code >= 400:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"Local SD WebUI {resp.status_code}: {resp.text[:300]}",
            )

        body = resp.json()
        b64_images = body.get("images") or []
        if not b64_images:
            return ImageGenResult(
                success=False, image_paths=[],
                error="WebUI returned no images.",
            )

        # The first N images are the actual generations; some WebUI
        # configs append a grid as the last entry — ignore extras when
        # we only asked for `num_images`.
        wanted = b64_images[: request.num_images]
        paths = self._write_b64_pngs(wanted, save_to=request.save_to)

        # WebUI puts seed + parameters in `info` (JSON-encoded string).
        info_raw = body.get("info") or "{}"
        info = {}
        try:
            import json as _json

            info = _json.loads(info_raw) if isinstance(info_raw, str) else info_raw
        except Exception:
            pass

        model_used = (
            info.get("sd_model_name")
            or self.default_model
            or "(WebUI default)"
        )
        return ImageGenResult(
            success=True,
            image_paths=paths,
            model_used=f"local-sd/{model_used}",
            cost_usd=0.0,
            raw={"info": info},
        )

    @staticmethod
    def _write_b64_pngs(b64_images: list[str], *, save_to: Path | None) -> list[Path]:
        if save_to is not None and save_to.suffix:
            target_dir = save_to.parent
        elif save_to is not None:
            target_dir = save_to
        else:
            target_dir = Path.home() / ".korpha" / "generated_images"
        target_dir = Path(target_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)

        paths: list[Path] = []
        for i, b64 in enumerate(b64_images):
            try:
                data = base64.b64decode(b64)
            except (ValueError, TypeError) as exc:
                logger.warning("local-sd: bad base64 image: %s", exc)
                continue
            if save_to is not None and save_to.suffix and i == 0:
                target = save_to
            else:
                from uuid import uuid4

                target = target_dir / f"local_{uuid4().hex[:12]}.png"
            target.write_bytes(data)
            paths.append(target)
        return paths

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["LocalSDProvider"]
