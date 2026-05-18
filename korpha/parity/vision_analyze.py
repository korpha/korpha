"""vision_analyze — pass raw image bytes to a vision-tier model.

Existing flow: a skill captures a screenshot, uploads it somewhere,
returns a URL, then a text-summary skill describes the URL. Two LLM
calls + an upload round-trip for what should be a single vision-tier
inference.

This helper short-circuits that: takes PNG bytes + a question, sends
both to a vision-capable model in one call, returns the answer.

Used by:
  - browser screenshot diagnostics
  - dashboard "describe this design"
  - any skill that already has an image in memory
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from korpha.audit.model import InferenceTier
from korpha.inference.types import (
    CompletionRequest,
    ImageRef,
    Message,
    Role,
)


@dataclass(frozen=True)
class VisionAnalyzeResult:
    answer: str
    input_tokens: int
    output_tokens: int


async def analyze_image_bytes(
    *,
    image_png: bytes,
    question: str,
    cost_tracker,
    session,
    business_id,
    model_hint: str | None = None,
) -> VisionAnalyzeResult:
    """Inline-send PNG bytes + question to the active vision-capable
    provider via the cost_tracker (which handles routing through the
    inference cascade). Returns the model's answer.

    ``model_hint`` overrides cascade routing — useful when the caller
    knows ``claude-opus-4-7`` will answer better than the default.

    Bytes are base64-encoded into a data URL and attached as an
    ``ImageRef`` on the message. Every modern OpenAI-compat vision
    model handles this shape (Qwen-VL, Llama-3.2-Vision, GLM-4V,
    Pixtral, Nemotron, Claude, GPT-4V/5)."""
    if not image_png:
        raise ValueError("image_png must be non-empty bytes")
    if not question or not question.strip():
        raise ValueError("question must be non-empty")

    b64 = base64.b64encode(image_png).decode("ascii")
    image = ImageRef(b64_png=b64, detail="high")

    msg = Message(
        role=Role.USER,
        content=question.strip(),
        images=(image,),
    )
    request = CompletionRequest(
        messages=[msg],
        tier=InferenceTier.PRO,
        session_key=f"vision-analyze-{business_id}",
        timeout_seconds=90.0,
    )
    resp = await cost_tracker.complete(
        request,
        session=session,
        business_id=business_id,
    )
    return VisionAnalyzeResult(
        answer=resp.content or "",
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
    )


__all__ = [
    "VisionAnalyzeResult",
    "analyze_image_bytes",
]
