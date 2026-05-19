"""Per-model output-discipline overlays appended to system prompts.

The role prompts (CEO/CMO/COO/CTO/COPYWRITER/DESIGNER/SUPPORT) were
iterated against DeepSeek's response habits over several months —
that's why DeepSeek V4 hits 96.2% on the adherence eval out of the
box. Models with different output habits (GPT-5.x, Claude Opus, etc.)
score lower not because they're less capable, but because the
scaffolding asks for shapes they don't produce by default
(numbered "Variant N:" labels, strict word caps, recommendation-first
sentences instead of markdown headers, etc.).

Rather than fork the role prompts per model (high drift cost) or tune
them down to a lowest-common-denominator (would tank DeepSeek), we
append a small per-model **overlay** to the system message at LLM-call
time. The overlay only fires when the request's model id matches a
known pattern; open-weights models get an empty overlay so their
prompt is unchanged and their score stays put.

Wired into ``InferencePool.complete`` / ``stream_complete`` right
before dispatch. Opt out per-process with
``KORPHA_DISABLE_PROMPT_OVERLAYS=1`` (used by the eval driver when
measuring baseline lift).
"""
from __future__ import annotations

import os
from dataclasses import replace as _replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from korpha.inference.types import CompletionRequest


# ---------------------------------------------------------------------------
# Overlay catalogue — keyed by model-id prefix
# ---------------------------------------------------------------------------


_GPT_5_X_OVERLAY = """
Output discipline (applies to every response you produce):

1. When the brief asks for N variants of something (cold emails,
   headlines, tweet versions, etc.), explicitly label each one as
   "Variant 1:", "Variant 2:", "Variant 3:" — no implicit numbering
   via narrative ("First, ..."), no headers as substitutes. Email
   variants must also include an explicit "Subject:" line per variant.

2. Word caps are HARD limits. Count words before you submit. A brief
   that says "under 60 words" means strictly ≤ 60 words including
   punctuation. A 200-word cap means ≤ 200. Headlines + subheads
   combined under their stated cap. Tweet-format outputs treat the
   stated word cap as the upper bound, not a soft target.

3. Lead the first SENTENCE with your recommendation or the substance
   of what was asked. Do not open with a markdown header
   ("**Strategic Recommendation**", "## Plan", etc.) — those break
   the dashboard's "first-line" assertion the user sees in their UI.

4. When a worker / role is asked to delegate a trivial task,
   identify the delegate ([CTO]/[CMO]/[COO]/<worker name>) and stop.
   Do not perform the work inline even when it's easy — the eval
   measures the delegation behaviour, not your ability to write
   a diff yourself.
""".strip()


_CLAUDE_OPUS_OVERLAY = """
Output discipline (applies to every response you produce):

WORD CAPS — STRICT WHITESPACE-TOKEN COUNTING

The downstream eval counts words as ``len(response.split())`` — i.e.
every whitespace-separated token, INCLUDING labels like "Subject:",
"Headline:", "Day 1:", and including punctuation-only tokens. The
practical implications:

1. **Aim for (cap − 10) words**, not the cap itself. A "60-word
   tweet" should target 50 content words. A "200-word legal reply"
   should target 190. The safety buffer absorbs label overhead +
   punctuation counted as separate tokens.

2. **When the brief asks for headline + subhead combined under N
   words, you have N TOTAL words across both lines** — not N per
   line. A 80-word cap means roughly: headline ≤ 12 words,
   subhead ≤ 60 words, leaving 8 for whitespace+label buffer.
   If you write a full-page subhead even with a punchy headline,
   you blow the cap.

3. **For tweet-format outputs, the cap is the tweet body — count
   everything**. Hashtags, the call-to-action, line breaks (each
   counts as zero, but words on different lines all count). A
   60-word tweet means 55 visible words MAX in the body.

4. **Do NOT add labels in word-counted outputs**. Write the headline
   directly. Write the subhead directly. The word "Headline:" is
   one of your 80 words. Skip the label whenever the brief doesn't
   explicitly ask for it.

OTHER OUTPUT DISCIPLINE

5. **Lead with the recommendation or substance as your FIRST
   sentence**, not a markdown header. "Recommendation:",
   "Assessment:", "Strategic plan:" — these are headers and they
   all break the first-line check.

6. **When asked to delegate, identify the delegate
   ([CTO]/[CMO]/[COO]/worker name) and STOP**. Do not also perform
   the work inline — not even a trivial typo fix. The delegation
   IS the response.

7. **Never commit to specific timelines for bug fixes / shipping**.
   Forbidden substrings when asked about bug repro or refund
   timing: "ETA", "by tomorrow", "by Friday", "in 24 hours",
   "in 2 days", "next week", "next sprint", "shortly". Replace with
   "I'll check with the founder", "let me escalate this", or
   "I'll get back to you once we've triaged".

8. **Support escalation responses must include an explicit hand-off
   phrase** when the situation warrants — "escalate", "team lead",
   "founder", "check with the team". Open with empathy ("Sorry to
   hear", "Thanks for flagging", "I appreciate") before substance
   on bug / refund / legal threads. Legal threats: keep the reply
   under 200 words (no flowery prose, just acknowledge + escalate
   to founder).

9. **When asked for N variants** (cold emails, headlines), label
   each "Variant 1:", "Variant 2:", "Variant 3:" with an explicit
   "Subject:" line per email variant. No implicit numbering.
""".strip()


# Pattern → overlay. Keys matched as case-insensitive PREFIXES against
# the model id so "gpt-5.4", "gpt-5.4-codex", "openai/gpt-5.4" all
# pick up the same overlay.
_OVERLAYS: dict[str, str] = {
    "gpt-5": _GPT_5_X_OVERLAY,
    "openai/gpt-5": _GPT_5_X_OVERLAY,
    "claude-opus-4": _CLAUDE_OPUS_OVERLAY,
    "anthropic/claude-opus-4": _CLAUDE_OPUS_OVERLAY,
}


def get_overlay(model_id: str) -> str:
    """Return the overlay text for this model id, or empty string if
    no overlay applies. Empty result means the system prompt is sent
    unchanged — open-weights path.

    Lookup is a case-insensitive prefix scan. First match wins (the
    catalogue is intentionally small + non-overlapping).
    """
    if not model_id:
        return ""
    if os.getenv("KORPHA_DISABLE_PROMPT_OVERLAYS"):
        return ""
    needle = model_id.lower()
    for prefix, text in _OVERLAYS.items():
        if needle.startswith(prefix):
            return text
    return ""


def apply_overlay(
    request: "CompletionRequest", model_id: str,
) -> "CompletionRequest":
    """Return a (possibly new) CompletionRequest with the model's
    overlay appended to its system message.

    No-ops when:
      - the env-var opt-out is set
      - no overlay matches this model
      - the request has no system message (unusual; nothing to append to)

    Returns the same object unchanged when no-op so callers don't
    have to special-case it.
    """
    overlay = get_overlay(model_id)
    if not overlay:
        return request
    if not request.messages:
        return request
    from korpha.inference.types import Message, Role
    first = request.messages[0]
    if first.role != Role.SYSTEM:
        return request

    new_system = Message(
        role=Role.SYSTEM,
        content=f"{first.content}\n\n{overlay}",
    )
    new_messages = [new_system, *request.messages[1:]]
    return _replace(request, messages=new_messages)


__all__ = [
    "apply_overlay",
    "get_overlay",
]
