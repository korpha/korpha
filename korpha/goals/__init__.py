"""Persistent goals — the Ralph loop for Korpha.

A goal is a free-form founder objective that stays active across
turns on a thread. After each agent reply, an aux-LLM judge asks
"is this goal satisfied?". If not, we feed a continuation prompt
back into the same thread and keep working until the goal is
done, the turn budget runs out, the founder pauses/clears it, or
a real founder message preempts.

Adapted from Hermes' ``hermes_cli/goals.py`` (Tenacity release
v0.13.0). Same judge contract + auto-pause-after-3-parse-failures
backstop. Korpha differences:

  - State persisted as a proper ``Goal`` SQLModel (per thread),
    not a key-value blob in a session-meta table.
  - Judge runs through the existing ``CostTracker`` so it shows
    up in the audit trail + insights digest. Override per-task
    via ``korpha/auxiliary.yaml`` (``goal-judge:`` prefix).
  - Founder-mediated semantics: a real new message from the
    founder always preempts the loop and pauses for the next
    turn (we re-judge after — if their message happened to
    complete the goal, the judge says done).
"""
from korpha.goals.judge import (
    DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES,
    DEFAULT_MAX_TURNS,
    JUDGE_SYSTEM_PROMPT,
    JudgeVerdict,
    parse_judge_response,
)
from korpha.goals.manager import (
    GoalManager,
    GoalReplaceConflict,
    continuation_prompt_for,
)
from korpha.goals.model import Goal, GoalStatus
from korpha.goals.slash import (
    GoalSlashIntent,
    execute_goal_slash,
    is_goal_slash,
    parse_goal_slash,
)

__all__ = [
    "DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES",
    "DEFAULT_MAX_TURNS",
    "Goal",
    "GoalManager",
    "GoalReplaceConflict",
    "GoalSlashIntent",
    "GoalStatus",
    "JUDGE_SYSTEM_PROMPT",
    "JudgeVerdict",
    "continuation_prompt_for",
    "execute_goal_slash",
    "is_goal_slash",
    "parse_goal_slash",
]
