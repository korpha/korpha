"""Eval fixture + result types.

Fixture files live as YAML under ``korpha/evals/fixtures/<role>.yaml``.
Each fixture has:

  - ``id``: stable identifier (referenced in scorecards)
  - ``role``: ceo | cto | cmo | coo (drives which system prompt loads)
  - ``ask``: the user message the role receives
  - ``assertions``: list of ``{kind, ...}`` dicts → checked against the
    LLM response

Assertion kinds + parameters are documented in
``korpha/evals/assertions.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Assertion:
    """One deterministic check against an LLM response.

    ``kind`` selects which checker function runs (see
    korpha.evals.assertions.CHECKERS). ``params`` are kind-specific —
    e.g. ``{"value": "what do you want", "case_insensitive": true}``
    for ``kind: not_contains``.
    """

    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    """Human-readable label shown in the scorecard."""


@dataclass(frozen=True)
class TaskFixture:
    """One eval task — a single founder-ask + the assertions to run on
    the model's response."""

    id: str
    role: str
    """One of ``ceo`` / ``cto`` / ``cmo`` / ``coo``."""
    ask: str
    """The user-message text. The runner pairs this with the role's
    system prompt loaded from korpha/cofounder."""
    assertions: tuple[Assertion, ...]
    notes: str = ""
    """Optional hint for fixture authors / future readers."""


@dataclass
class AssertionResult:
    assertion: Assertion
    passed: bool
    detail: str = ""
    """Why it failed — only populated when ``passed=False``."""


@dataclass
class TaskRunResult:
    task: TaskFixture
    response: str
    """Raw model response — saved for debugging the score."""
    results: list[AssertionResult]
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    """Set when the model call failed (provider error, timeout, etc.)
    instead of returning a response. ``results`` will be empty in that
    case."""

    @property
    def pass_rate(self) -> float:
        """Fraction of assertions that passed. 0.0 when error or no
        assertions."""
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.passed) / len(self.results)


@dataclass
class RoleScorecard:
    """Aggregate across every task for one role."""

    role: str
    tasks: list[TaskRunResult]

    @property
    def total_assertions(self) -> int:
        return sum(len(t.results) for t in self.tasks)

    @property
    def passed_assertions(self) -> int:
        return sum(
            sum(1 for r in t.results if r.passed) for t in self.tasks
        )

    @property
    def pass_rate(self) -> float:
        if self.total_assertions == 0:
            return 0.0
        return self.passed_assertions / self.total_assertions

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.tasks)


@dataclass
class EvalReport:
    """Top-level scorecard. Includes per-role rollup + provider tag."""

    provider_label: str
    """Where the responses came from. Used in ``korpha eval --diff``
    to compare runs."""
    roles: list[RoleScorecard]

    @property
    def overall_pass_rate(self) -> float:
        total = sum(r.total_assertions for r in self.roles)
        passed = sum(r.passed_assertions for r in self.roles)
        return passed / total if total else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(r.total_cost_usd for r in self.roles)


__all__ = [
    "Assertion",
    "AssertionResult",
    "EvalReport",
    "RoleScorecard",
    "TaskFixture",
    "TaskRunResult",
]
