"""Eval runner — load fixtures, call the LLM, score the response.

Uses the real ``cofounder_voice`` (CEO) and ``DirectorPersonality``
system prompts so the eval scores what we actually ship. Don't pass a
synthetic "you are a CEO" prompt here — that defeats the audit.

The runner is provider-agnostic: pass in any ``InferencePool`` +
``ProviderAccount`` you want to score. Recommended canonical baseline
is DeepSeek V4 Pro (open weights, frontier, what most users will run).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from korpha.audit.model import InferenceTier
from korpha.cofounder.ceo import _CEO_VOICE_DEFAULT
from korpha.cofounder.director import (
    CMO_PERSONALITY,
    COO_PERSONALITY,
    COPYWRITER_WORKER,
    CTO_PERSONALITY,
    DESIGNER_WORKER,
    SUPPORT_WORKER,
    DirectorPersonality,
    WorkerPersonality,
)
from korpha.evals.assertions import run_assertion
from korpha.evals.types import (
    Assertion,
    AssertionResult,
    EvalReport,
    RoleScorecard,
    TaskFixture,
    TaskRunResult,
)
from korpha.inference.limits import agent_max_tokens, agent_timeout
from korpha.inference.pool import InferencePool
from korpha.inference.registry import ProviderAccount
from korpha.inference.types import CompletionRequest, Message, Role

logger = logging.getLogger(__name__)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Director personalities indexed by lowercase role tag.
_DIRECTOR_PERSONALITIES: dict[str, DirectorPersonality] = {
    "cto": CTO_PERSONALITY,
    "cmo": CMO_PERSONALITY,
    "coo": COO_PERSONALITY,
}

# Worker personalities indexed by specialty (matches the fixture role tag).
_WORKER_PERSONALITIES: dict[str, WorkerPersonality] = {
    "copywriter": COPYWRITER_WORKER,
    "designer": DESIGNER_WORKER,
    "support": SUPPORT_WORKER,
}


def load_fixtures(role: str | None = None, *, root: Path | None = None) -> list[TaskFixture]:
    """Read every ``<role>.yaml`` under ``korpha/evals/fixtures/``.

    ``role`` filters to one role's fixtures; otherwise loads all four.
    """
    base = root or _FIXTURES_DIR
    if not base.exists():
        return []
    files = (
        [base / f"{role}.yaml"]
        if role
        else sorted(base.glob("*.yaml"))
    )
    out: list[TaskFixture] = []
    for f in files:
        if not f.exists():
            continue
        body = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        tasks = body.get("tasks") or []
        if not isinstance(tasks, list):
            continue
        for raw in tasks:
            out.append(_parse_fixture(raw, source=f))
    return out


def _parse_fixture(raw: Any, *, source: Path) -> TaskFixture:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: task must be a mapping, got {type(raw).__name__}")
    assertions_raw = raw.get("assertions") or []
    assertions = tuple(
        Assertion(
            kind=str(a.get("kind", "")),
            params={k: v for k, v in a.items() if k not in ("kind", "description")},
            description=str(a.get("description", "")),
        )
        for a in assertions_raw
        if isinstance(a, dict)
    )
    return TaskFixture(
        id=str(raw.get("id", f"{source.stem}.unknown")),
        role=str(raw.get("role", source.stem)).lower(),
        ask=str(raw.get("ask", "")).strip(),
        assertions=assertions,
        notes=str(raw.get("notes", "")),
    )


def _system_prompt_for_role(role: str) -> str:
    """Return the actual system prompt the cofounder uses for this role.

    The whole point of the eval is to score what we ship — we do NOT
    fabricate a synthetic "you are a CEO" string here.
    """
    role = role.lower()
    if role == "ceo":
        # Module-level constant — same string the CEO dataclass uses
        # by default. Eval scores the prompt we actually ship.
        return _CEO_VOICE_DEFAULT
    director = _DIRECTOR_PERSONALITIES.get(role)
    if director is not None:
        return director.system_prompt
    worker = _WORKER_PERSONALITIES.get(role)
    if worker is not None:
        return worker.system_prompt
    raise ValueError(
        f"Unknown role {role!r}. "
        f"Known: ceo, cto, cmo, coo, copywriter, designer, support."
    )


async def run_task(
    task: TaskFixture,
    *,
    pool: InferencePool,
    account: ProviderAccount,
    tier: InferenceTier = InferenceTier.PRO,
    timeout_seconds: float | None = None,
    max_tokens: int | None = None,
) -> TaskRunResult:
    """Run one fixture: build the role's actual prompt, call the LLM,
    evaluate every assertion. Captures provider errors as ``error`` on
    the result so the sweep doesn't crash on a single failure.

    ``max_tokens`` and ``timeout_seconds`` default to the global
    ``agent_max_tokens()`` / ``agent_timeout()`` floors so the eval
    scores the prompt under the same budget agents actually use in
    production. Override per-call only for narrow A/B tests.
    """
    if max_tokens is None:
        max_tokens = agent_max_tokens()
    if timeout_seconds is None:
        timeout_seconds = agent_timeout()
    try:
        system_prompt = _system_prompt_for_role(task.role)
    except ValueError as exc:
        return TaskRunResult(
            task=task, response="", results=[], error=str(exc),
        )

    request = CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content=system_prompt),
            Message(role=Role.USER, content=task.ask),
        ],
        tier=tier,
        session_key=f"eval:{task.id}",
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )

    # The pool routes by session_key + tier, so we don't pass account
    # explicitly. ``account`` is still in the signature to keep the
    # caller-facing intent clear (eval against this account's tier
    # mapping); the pool selects from its configured accounts.
    _ = account

    # Bounded retry on transient provider errors. A failed task used
    # to register as a flat 0/N for the task's assertions, which
    # silently dropped points on a momentary rate-limit / subprocess
    # crash / session race — punishing the MODEL for an INFRA glitch.
    # Three attempts with short backoff; if all three fail, then
    # surface the error (and only then deduct points). Aligns the eval
    # with production behavior where the cascade retries automatically.
    import asyncio
    _RETRY_ATTEMPTS = 3
    _RETRY_BACKOFF_SECONDS = (1.0, 3.0)  # between attempt 1→2, 2→3
    last_exc: Exception | None = None
    response = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = await pool.complete(request)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < _RETRY_ATTEMPTS:
                delay = _RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "eval task %s attempt %d/%d failed: %s — retrying in %.1fs",
                    task.id, attempt + 1, _RETRY_ATTEMPTS, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "eval task %s failed after %d attempts: %s",
                    task.id, _RETRY_ATTEMPTS, exc,
                )
    if response is None:
        return TaskRunResult(
            task=task, response="", results=[],
            error=str(last_exc) if last_exc else "unknown",
        )

    text = response.content
    results: list[AssertionResult] = []
    for a in task.assertions:
        passed, detail = run_assertion(text, a.kind, a.params)
        results.append(AssertionResult(assertion=a, passed=passed, detail=detail))
    return TaskRunResult(
        task=task,
        response=text,
        results=results,
        cost_usd=float(response.cost_usd),
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


async def run_eval(
    *,
    pool: InferencePool,
    account: ProviderAccount,
    provider_label: str,
    tasks: Iterable[TaskFixture] | None = None,
    role: str | None = None,
    tier: InferenceTier = InferenceTier.PRO,
    fixtures_root: Path | None = None,
    max_tokens: int | None = None,
) -> EvalReport:
    """Run the full eval and produce a scorecard.

    Pass ``tasks`` explicitly to run a custom set; otherwise we load
    fixtures from disk (filtered by ``role`` if given). ``provider_label``
    goes into the report so a later ``--diff`` knows what to compare.
    """
    if tasks is None:
        tasks = load_fixtures(role=role, root=fixtures_root)

    by_role: dict[str, list[TaskRunResult]] = {}
    for task in tasks:
        result = await run_task(
            task, pool=pool, account=account, tier=tier,
            max_tokens=max_tokens,
        )
        by_role.setdefault(task.role, []).append(result)

    return EvalReport(
        provider_label=provider_label,
        roles=[
            RoleScorecard(role=role, tasks=tasks)
            for role, tasks in sorted(by_role.items())
        ],
    )


def average_reports(reports: list[EvalReport]) -> EvalReport:
    """Combine N runs of the same fixture set into one averaged report.

    For each assertion, count how many runs passed it. An assertion is
    marked ``passed=True`` in the merged report when it passed in **at
    least the majority** of runs (``ceil(N/2)``). The detail line shows
    "X/N runs" so you can see borderline tasks.

    All reports must share the same fixture structure (same roles, same
    task ids, same assertion order). Caller is responsible for that —
    we trust the fact that all runs went through ``load_fixtures()``.
    """
    if not reports:
        raise ValueError("average_reports() needs at least one report")
    if len(reports) == 1:
        return reports[0]

    n_runs = len(reports)
    threshold = (n_runs + 1) // 2  # majority: ≥ ceil(n_runs/2)

    base = reports[0]
    out_roles: list[RoleScorecard] = []
    for r_idx, role_card in enumerate(base.roles):
        merged_tasks: list[TaskRunResult] = []
        for t_idx, task_run in enumerate(role_card.tasks):
            merged_assertions: list[AssertionResult] = []
            for a_idx, a_result in enumerate(task_run.results):
                pass_count = sum(
                    1 for rep in reports
                    if rep.roles[r_idx].tasks[t_idx].results[a_idx].passed
                )
                passed = pass_count >= threshold
                if passed:
                    detail = f"{pass_count}/{n_runs} runs passed"
                else:
                    sample_failure = next(
                        (
                            rep.roles[r_idx].tasks[t_idx].results[a_idx].detail
                            for rep in reports
                            if not rep.roles[r_idx].tasks[t_idx].results[a_idx].passed
                        ),
                        "",
                    )
                    detail = (
                        f"{pass_count}/{n_runs} runs passed; "
                        f"e.g. {sample_failure[:120]}"
                    )
                merged_assertions.append(
                    AssertionResult(
                        assertion=a_result.assertion,
                        passed=passed,
                        detail=detail,
                    )
                )
            avg_cost = sum(
                rep.roles[r_idx].tasks[t_idx].cost_usd for rep in reports
            ) / n_runs
            merged_tasks.append(
                TaskRunResult(
                    task=task_run.task,
                    response=task_run.response,
                    results=merged_assertions,
                    cost_usd=avg_cost,
                )
            )
        out_roles.append(
            RoleScorecard(role=role_card.role, tasks=merged_tasks)
        )

    return EvalReport(
        provider_label=f"{base.provider_label} (avg of {n_runs} runs)",
        roles=out_roles,
    )


def render_report(report: EvalReport) -> str:
    """Format an EvalReport as a readable scorecard for the CLI.

    Two sections: per-role rollup (one line each), then per-task detail
    (which assertions failed, with detail strings)."""
    lines: list[str] = []
    lines.append(f"Eval report — provider: {report.provider_label}")
    lines.append("=" * 60)
    lines.append("")
    lines.append("Per-role rollup:")
    for role in report.roles:
        pct = role.pass_rate * 100
        lines.append(
            f"  {role.role.upper():<6} "
            f"{role.passed_assertions:>3}/{role.total_assertions:<3} "
            f"({pct:5.1f}%)  ${role.total_cost_usd:.4f}"
        )
    lines.append("")
    lines.append(
        f"Overall: {sum(r.passed_assertions for r in report.roles)}"
        f"/{sum(r.total_assertions for r in report.roles)} "
        f"({report.overall_pass_rate * 100:.1f}%)  "
        f"total cost ${report.total_cost_usd:.4f}"
    )
    lines.append("")
    lines.append("Per-task detail:")
    for role in report.roles:
        for task_result in role.tasks:
            t = task_result.task
            if task_result.error:
                lines.append(f"  ✗ {t.id}  ERROR: {task_result.error}")
                continue
            n_pass = sum(1 for r in task_result.results if r.passed)
            n_total = len(task_result.results)
            mark = "✓" if n_pass == n_total else "✗"
            lines.append(f"  {mark} {t.id}  {n_pass}/{n_total}")
            for r in task_result.results:
                if not r.passed:
                    label = r.assertion.description or r.assertion.kind
                    lines.append(f"      ✗ {label}: {r.detail}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "EvalReport",
    "RoleScorecard",
    "TaskRunResult",
    "average_reports",
    "load_fixtures",
    "render_report",
    "run_eval",
    "run_task",
]
