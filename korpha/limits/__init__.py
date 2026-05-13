"""Output-size discipline: spillover + per-turn budget for tool results.

Three layers, in increasing scope:

  1. **Per-tool truncation** — the skill author's responsibility (e.g.
     ``research.scrape`` already trims long pages before returning).
  2. **Per-result spillover** (:func:`persist_if_oversized`) — when a
     single result still exceeds the threshold, full content goes to
     ``~/.korpha/tool_results/<ref_id>.txt`` and the model sees a
     preview + path. The model can ``read_file`` the path if it
     actually needs the rest. Cuts most cost-bombs.
  3. **Per-turn aggregate budget** (:func:`enforce_turn_budget`) —
     when many medium-sized results combine to overflow, we spill the
     largest non-persisted ones until the aggregate is under budget.
     Catches the "fanned-out workforce dispatched 6 directors and
     each returned 30KB" scenario.

Defaults:

  - ``PERSIST_THRESHOLD_CHARS`` — 16,000 (≈ 4K tokens). One result
    above this gets spilled.
  - ``PREVIEW_CHARS`` — 4,000 (≈ 1K tokens). What the model sees in
    place of the full payload.
  - ``TURN_BUDGET_CHARS`` — 200,000 (≈ 50K tokens). Aggregate cap.

Override for testing or per-deploy tuning via env vars:
``KORPHA_PERSIST_THRESHOLD_CHARS``, ``KORPHA_PREVIEW_CHARS``,
``KORPHA_TURN_BUDGET_CHARS``.
"""
from korpha.limits.output_budget import (
    PERSIST_THRESHOLD_CHARS,
    PERSISTED_OUTPUT_TAG,
    PREVIEW_CHARS,
    TURN_BUDGET_CHARS,
    enforce_turn_budget,
    is_persisted,
    persist_if_oversized,
    serialize_for_prompt,
)

__all__ = [
    "PERSISTED_OUTPUT_TAG",
    "PERSIST_THRESHOLD_CHARS",
    "PREVIEW_CHARS",
    "TURN_BUDGET_CHARS",
    "enforce_turn_budget",
    "is_persisted",
    "persist_if_oversized",
    "serialize_for_prompt",
]
