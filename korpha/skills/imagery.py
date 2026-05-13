"""imagery.generate_image — render an image via the configured backend.

Routes through ``ImageGenService`` which picks from whatever the user
configured in ``providers.yaml`` under ``image_providers:`` (Replicate,
fal.ai, local SD WebUI, Codex CLI). Falls back to a pure Codex-CLI
provider only if nothing's configured AND ``codex`` is on PATH — that
keeps the skill working out of the box for users who already have
Codex, without making it the only option.

The Founder's ``founder_brief`` is consulted indirectly via the prompt
the caller supplies; this skill itself is provider-agnostic.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from korpha.audit.model import InferenceTier
from korpha.imagery import (
    CodexCLIImageProvider,
    ImageGenRequest,
)
from korpha.imagery.service import ImageGenService, load_image_providers
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)


class GenerateImageSkill(Skill):
    spec = SkillSpec(
        name="imagery.generate_image",
        description=(
            "Generate an image from a text prompt via the configured "
            "image-gen backend (Replicate / fal.ai / local SD WebUI / "
            "Codex CLI). Returns the local file path."
        ),
        parameters={
            "prompt": (
                "What to generate, e.g. 'minimal logo for a deployment-"
                "automation SaaS, sage green on cream'"
            ),
            "style_hint": (
                "Optional: 'photorealistic' | 'illustration' | 'minimal' | "
                "'isometric' | etc. Folded into the prompt."
            ),
            "negative_prompt": "Optional: things to avoid (most local + "
                "open-weights models honor this).",
            "width": "Optional: pixels (default 1024).",
            "height": "Optional: pixels (default 1024).",
            "num_images": "Optional: how many to render (default 1).",
            "save_to": (
                "Optional absolute path or directory. If a path with an "
                "extension, the first image is saved there. If a "
                "directory, all results are dropped in. Default: "
                "~/.korpha/generated_images/"
            ),
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        del ctx  # provider-agnostic; nothing from SkillContext is needed
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise SkillError(
                "imagery.generate_image requires a non-empty 'prompt'."
            )

        save_to_raw = str(args.get("save_to") or "").strip() or None
        save_to = Path(save_to_raw).expanduser() if save_to_raw else None

        request = ImageGenRequest(
            prompt=prompt,
            negative_prompt=str(args.get("negative_prompt") or "").strip() or None,
            width=int(args.get("width") or 1024),
            height=int(args.get("height") or 1024),
            num_images=int(args.get("num_images") or 1),
            style_hint=str(args.get("style_hint") or "").strip() or None,
            save_to=save_to,
        )

        service = _build_service()
        try:
            result = await service.generate(request)
        finally:
            await service.close()

        if not result.success or not result.image_paths:
            raise SkillError(
                result.error
                or "Image generation failed (no provider returned an image)."
            )

        primary = result.image_paths[0]
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Image saved to {primary}",
            payload={
                "image_path": str(primary),
                "image_paths": [str(p) for p in result.image_paths],
                "model_used": result.model_used,
                "cost_usd": result.cost_usd,
                "byte_size": primary.stat().st_size if primary.exists() else 0,
                "raw": result.raw,
            },
            cost_usd=float(result.cost_usd),
        )


def _build_service() -> ImageGenService:
    """Pull configured image providers from providers.yaml. Falls back
    to Codex CLI if nothing's configured AND the binary is on PATH —
    keeps the skill working out of the box for the historic case."""
    providers = load_image_providers()
    if not providers and shutil.which("codex") is not None:
        providers = [CodexCLIImageProvider()]
    return ImageGenService(providers=providers)


# Module-level instance + registration — matches the other built-in skills.
register(GenerateImageSkill())


__all__ = ["GenerateImageSkill"]
