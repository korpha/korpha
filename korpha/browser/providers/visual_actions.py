"""Vision-driven action step — screenshot → pixel-coord JSON.

The accessibility-tree step is fast + cheap but falls down on sites
with heavy shadow DOMs, custom contenteditable surfaces, or aggressive
React virtualization (LinkedIn compose, Instagram modals, YouTube
Studio). This module gives the action loop a fallback step that:

  1. Captures a full-page PNG screenshot via Playwright.
  2. Sends it to a vision-capable LLM with a strict JSON schema:
     ``click_xy``, ``type_at``, ``scroll``, ``navigate``, ``done``,
     ``abort``, ``key``.
  3. The action loop's ``_execute_action`` then drives Playwright's
     mouse + keyboard at the returned coordinates.

The fallback is engaged by ``PlaywrightActionProvider`` when
``task.visual_fallback`` is True AND the acc-tree snapshot is empty
(or has been empty for a step). It is NOT the default — most pages
are still cheaper to drive via the accessibility tree.

Model selection: the inference pool dispatches based on tier. The
caller passes the same tier they use for acc-tree steps (PRO);
the pool picks whichever provider in that tier supports image
input. Falls back with a clear error if no vision-capable provider
is configured.
"""
from __future__ import annotations

import base64
from typing import Any

from korpha._jsonext import extract_json_dict
from korpha.audit.model import InferenceTier
from korpha.browser.service import BrowserError, BrowserTask
from korpha.inference.limits import agent_max_tokens
from korpha.inference.pool import InferencePool
from korpha.inference.types import (
    CompletionRequest,
    ImageRef,
    Message,
    Role,
)


_VISUAL_SYSTEM_PROMPT = """\
You are driving a Chromium browser one step at a time on behalf of an
AI cofounder. You receive a screenshot of the current page and must
return ONE action as strict JSON (no markdown, no commentary).

Allowed actions:
  {"action": "click_xy", "x": 540, "y": 312}
  {"action": "type_at", "x": 540, "y": 312, "text": "hello", "submit": false}
  {"action": "key", "text": "Enter"}             # named key press
  {"action": "navigate", "url": "https://..."}
  {"action": "scroll", "direction": "down"}      # or "up"
  {"action": "done", "result": "what you accomplished"}
  {"action": "abort", "reason": "why you stopped"}

Coordinate rules:
  - x and y are pixels measured from the TOP-LEFT of the screenshot.
  - Aim for the visual center of the target (button, field, link).
  - Coordinates must be inside the screenshot dimensions you can see.

Behaviour rules:
  - One action per response. Never a list.
  - Use ``done`` as soon as the goal is met.
  - Use ``abort`` if the page asks for login, hits a captcha, or a
    required element isn't visible (consider scrolling first).
  - type_at clicks then types. Set submit=true only if you also want
    Enter pressed at the end.
  - Don't speculate about what's off-screen; scroll if you need to.
"""


async def run_visual_step(
    *,
    page: Any,
    task: BrowserTask,
    pool: InferencePool,
    business_id: Any,
    step: int,
    max_steps: int,
) -> tuple[Any, float]:
    """Run one screenshot-driven step.

    Returns the parsed action + the step's cost in USD (consistent
    with the acc-tree path). The caller — ``PlaywrightActionProvider``
    — is responsible for dispatching the action against the page.

    Routes through the VISION tier (separate provider rotation from
    PRO so prompt-cache affinity isn't disturbed for the acc-tree
    rounds). Imports the action parser from ``playwright_action`` so
    both step types share the same action shape.
    """
    from korpha.browser.providers.playwright_action import _parse_action

    screenshot_png = await page.screenshot(type="png", full_page=False)
    img_b64 = base64.b64encode(screenshot_png).decode("ascii")

    user_prompt = (
        f"Goal: {task.instruction}\n"
        f"Current step: {step} / {max_steps}\n\n"
        f"Screenshot attached. What's the single next action?"
    )

    request = CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content=_VISUAL_SYSTEM_PROMPT),
            Message(
                role=Role.USER,
                content=user_prompt,
                images=(ImageRef(b64_png=img_b64, detail="high"),),
            ),
        ],
        tier=InferenceTier.VISION,
        session_key=f"browser-visual-{business_id}",
        max_tokens=agent_max_tokens(),
        temperature=0.1,
        timeout_seconds=task.timeout_seconds,
    )
    response = await pool.complete(request)
    parsed = extract_json_dict(response.content)
    if parsed is None:
        raise BrowserError(
            "visual step: model returned unparseable output. "
            f"first 300 chars: {response.content[:300]}"
        )
    return _parse_action(parsed), float(response.cost_usd)


__all__ = ["run_visual_step"]
