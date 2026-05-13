"""Deterministic role-prompt evals.

What this is for:

  - **Before/after**: did the prompt-pattern lift in
    ``docs/PROMPT_AUDIT.md`` actually improve outputs, or was it a
    ritual? Run the same fixtures against old vs new prompts.
  - **Open-weights model comparison**: same fixtures, swept across
    DeepSeek V4 Pro, Kimi K2.6, Qwen3.5, GLM-5.1, Llama 3.3 70B,
    Mistral Small 4. Generate a per-role table for the README.

Methodology borrowed from ClawEval (deterministic, exact-expected
assertions, no LLM-as-judge). Fixtures are Korpha-specific because
ClawEval's 1,220 checkpoints are narrow agent tasks (route tickets,
review code, plan sprints), not cofounder-shaped roles.

Recommended canonical baseline: DeepSeek V4 Pro.
Run with ``korpha eval --tier pro``.
"""
from korpha.evals.runner import (
    EvalReport,
    RoleScorecard,
    TaskRunResult,
    run_eval,
)
from korpha.evals.types import (
    Assertion,
    AssertionResult,
    TaskFixture,
)

__all__ = [
    "Assertion",
    "AssertionResult",
    "EvalReport",
    "RoleScorecard",
    "TaskFixture",
    "TaskRunResult",
    "run_eval",
]
