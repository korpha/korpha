"""Small parity-pack ports — Hermes v2026.5.16 catch-ups.

Four independent features bundled because each is small (~50-200 LOC)
and shares no infrastructure. Splitting into 4 PRs would be churn.

  * ``session_handoff`` — move an active session (messages + context)
    to a different model/persona mid-run. ``/handoff claude-opus``.
    Mirrors Hermes PR #23395.

  * ``subgoal`` — append acceptance criteria to the active /goal
    without resetting the loop. ``/subgoal "and write tests"``.
    Mirrors Hermes PR #25449.

  * ``pareto_router`` — pick the cheapest OpenRouter model that meets
    a min_coding_score. Caller passes the threshold; we return the
    model id + cost estimate. Mirrors Hermes PR #22838.

  * ``vision_analyze`` — pass raw image bytes to a vision-tier model
    instead of round-tripping through a text-summary skill.
    Mirrors Hermes PR #22955.
"""
from korpha.parity.pareto_router import (
    ParetoModel,
    pick_pareto_model,
)
from korpha.parity.session_handoff import (
    HandoffResult,
    SessionHandoff,
)
from korpha.parity.subgoal import (
    SubgoalEntry,
    append_subgoal,
    render_active_subgoals,
)
from korpha.parity.vision_analyze import (
    VisionAnalyzeResult,
    analyze_image_bytes,
)

__all__ = [
    "HandoffResult",
    "ParetoModel",
    "SessionHandoff",
    "SubgoalEntry",
    "VisionAnalyzeResult",
    "analyze_image_bytes",
    "append_subgoal",
    "pick_pareto_model",
    "render_active_subgoals",
]
