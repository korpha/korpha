"""Compute weekly / monthly cofounder insights from audit + cost rows.

Single function entry point :func:`compute_insights` returns a
``InsightsReport`` dataclass. The dashboard renders it as a panel,
the ``korpha insights`` CLI renders it as a terminal block, and
future weekly-digest skills can email the same numbers.

Hours-saved estimate is intentionally conservative — Mike will be
the first to call out "no way it actually saved me 40 hours" if
the math is generous. We use minutes-per-skill-call defaults that
err low; tunable via env var if a deploy wants different numbers.
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlmodel import Session, select


@dataclass(frozen=True)
class ProviderBreakdown:
    """How much we spent at each (provider, model) pair."""

    provider: str
    model: str
    tier: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class SkillUsage:
    """How many times each skill ran in the window."""

    skill_name: str
    calls: int
    role: str
    """The director that invoked it (CEO / CTO / CMO / COO / WORKER)
    when known, else 'unknown'."""


@dataclass(frozen=True)
class InsightsReport:
    """Aggregate cofounder activity for a time window. Frozen so the
    dashboard / CLI / digest can pass it around safely."""

    business_id: UUID
    window_start: datetime
    window_end: datetime
    window_days: int

    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int

    inference_calls: int
    """Total number of LLM calls made in the window."""

    skills_run: int
    """Number of skill invocations."""

    active_days: int
    """How many distinct calendar days had any activity. ``window_days``
    minus this = idle days, which is the more interesting framing
    for the "is the founder using this?" question."""

    by_provider: tuple[ProviderBreakdown, ...] = field(default_factory=tuple)
    """Sorted by cost descending — top spenders first."""

    top_skills: tuple[SkillUsage, ...] = field(default_factory=tuple)
    """Sorted by call count descending; capped to 10 to keep output
    skimmable."""

    estimated_hours_saved: float = 0.0
    """Best-effort heuristic. Tunable via KORPHA_INSIGHTS_MIN_PER_SKILL
    env var (default 6 minutes per skill call)."""

    def cost_per_day(self) -> float:
        """Average daily cost over the window. Useful for "if I keep
        running this, what's my monthly?" extrapolation."""
        if self.window_days <= 0:
            return 0.0
        return self.total_cost_usd / self.window_days

    def headline(self) -> str:
        """One-line marketing-friendly summary. 'Your cofounder cost
        $X, ran Y skills, saved you ~Zh' — the screenshot money shot."""
        cost_str = (
            f"${self.total_cost_usd:.2f}"
            if self.total_cost_usd >= 0.01
            else f"${self.total_cost_usd:.4f}"
        )
        hours = self.estimated_hours_saved
        if hours >= 1.0:
            saved = f"saved you ~{hours:.1f}h"
        else:
            saved = f"saved you ~{int(hours * 60)}m"
        return (
            f"Your cofounder cost {cost_str}, ran {self.skills_run} "
            f"skills, {saved} (last {self.window_days}d)"
        )


# ----------------------- Hours-saved heuristic -----------------------


def _minutes_per_skill_default() -> float:
    """Conservative default: 6 minutes per skill call. Means 10
    skill calls = 1 hour saved. Tunable via env so a deploy can
    err higher / lower if their skill mix justifies it."""
    raw = os.environ.get("KORPHA_INSIGHTS_MIN_PER_SKILL")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return 6.0


def estimate_hours_saved(
    skills_run: int,
    inference_calls: int,
    *,
    minutes_per_skill: float | None = None,
) -> float:
    """Convert skill + inference counts into an hours-saved number.

    Formula: each skill call ≈ N minutes of human work the founder
    would have done themselves; each inference-only call (e.g.
    chatting / strategy) ≈ N/3 minutes. Tunable defaults err low.

    Returns 0 for zero activity (don't claim time saved when nothing
    happened).
    """
    if skills_run <= 0 and inference_calls <= 0:
        return 0.0
    minutes_per_skill = (
        minutes_per_skill if minutes_per_skill is not None
        else _minutes_per_skill_default()
    )
    skill_minutes = skills_run * minutes_per_skill
    chat_minutes = max(0, inference_calls - skills_run) * (
        minutes_per_skill / 3.0
    )
    return (skill_minutes + chat_minutes) / 60.0


# ----------------------- Main aggregator ----------------------------


def compute_insights(
    session: Session,
    *,
    business_id: UUID,
    window_days: int = 7,
    now: datetime | None = None,
) -> InsightsReport:
    """Aggregate Activity + Cost rows for ``business_id`` over the
    last ``window_days`` days. Tests pass ``now`` to pin the window
    end deterministically; production passes nothing and uses the
    current time."""
    from korpha.audit.model import Activity, Cost

    end = now if now is not None else datetime.now(tz=timezone.utc)
    start = end - timedelta(days=window_days)

    cost_rows = list(session.exec(
        select(Cost)
        .where(Cost.business_id == business_id)
        .where(Cost.created_at >= start)
        .where(Cost.created_at <= end)
    ).all())

    activity_rows = list(session.exec(
        select(Activity)
        .where(Activity.business_id == business_id)
        .where(Activity.created_at >= start)
        .where(Activity.created_at <= end)
    ).all())

    total_cost = sum(
        (Decimal(str(c.cost_usd)) for c in cost_rows), Decimal("0"),
    )
    total_input = sum(c.input_tokens for c in cost_rows)
    total_output = sum(c.output_tokens for c in cost_rows)
    total_cached = sum(c.cached_tokens for c in cost_rows)
    inference_calls = len(cost_rows)

    by_pair: dict[
        tuple[str, str, str],
        dict[str, Any],
    ] = {}
    for row in cost_rows:
        key = (row.provider, row.model, row.tier.value)
        bucket = by_pair.setdefault(key, {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": Decimal("0"),
        })
        bucket["calls"] += 1
        bucket["input_tokens"] += row.input_tokens
        bucket["output_tokens"] += row.output_tokens
        bucket["cost_usd"] += Decimal(str(row.cost_usd))
    by_provider = tuple(sorted(
        (
            ProviderBreakdown(
                provider=k[0],
                model=k[1],
                tier=k[2],
                calls=v["calls"],
                input_tokens=v["input_tokens"],
                output_tokens=v["output_tokens"],
                cost_usd=float(v["cost_usd"]),
            )
            for k, v in by_pair.items()
        ),
        key=lambda b: b.cost_usd,
        reverse=True,
    ))

    skill_calls = [
        a for a in activity_rows
        if a.event_type in ("skill.invoked", "skill.completed")
    ]
    # Prefer skill.completed so we count each invocation once. If
    # only skill.invoked exists (older rows), fall back.
    completed_count = sum(
        1 for a in skill_calls if a.event_type == "skill.completed"
    )
    skills_run = completed_count or sum(
        1 for a in skill_calls if a.event_type == "skill.invoked"
    )

    skill_counter: Counter[tuple[str, str]] = Counter()
    for a in skill_calls:
        if a.event_type != "skill.completed" and completed_count:
            continue
        name = str(
            (a.payload or {}).get("skill_name")
            or (a.payload or {}).get("skill")
            or "unknown",
        )
        role = str((a.payload or {}).get("role") or "unknown")
        skill_counter[(name, role)] += 1
    top_skills = tuple(
        SkillUsage(skill_name=name, role=role, calls=count)
        for (name, role), count in skill_counter.most_common(10)
    )

    active_dates = {
        a.created_at.date()
        for a in activity_rows if a.created_at is not None
    }
    active_dates.update(
        c.created_at.date()
        for c in cost_rows if c.created_at is not None
    )
    active_days = len(active_dates)

    hours = estimate_hours_saved(skills_run, inference_calls)

    return InsightsReport(
        business_id=business_id,
        window_start=start,
        window_end=end,
        window_days=window_days,
        total_cost_usd=float(total_cost),
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_cached_tokens=total_cached,
        inference_calls=inference_calls,
        skills_run=skills_run,
        active_days=active_days,
        by_provider=by_provider,
        top_skills=top_skills,
        estimated_hours_saved=hours,
    )


# ----------------------- Terminal renderer --------------------------


def render_insights_terminal(
    report: InsightsReport, *, color: bool = True,
) -> str:
    """Render a compact human-readable block for the CLI. Color is
    ANSI escapes; pass ``color=False`` for piping or tests."""
    bold = "\x1b[1m" if color else ""
    dim = "\x1b[2m" if color else ""
    green = "\x1b[32m" if color else ""
    reset = "\x1b[0m" if color else ""

    lines: list[str] = []
    lines.append(f"{bold}{report.headline()}{reset}")
    lines.append("")
    lines.append(
        f"  {dim}Active days:{reset}      "
        f"{report.active_days}/{report.window_days}"
    )
    lines.append(
        f"  {dim}Inference calls:{reset}  {report.inference_calls}"
    )
    lines.append(
        f"  {dim}Tokens:{reset}           "
        f"{report.total_input_tokens:,} in / "
        f"{report.total_output_tokens:,} out"
        + (
            f" ({report.total_cached_tokens:,} cached)"
            if report.total_cached_tokens else ""
        )
    )
    lines.append(
        f"  {dim}Cost / day:{reset}       ${report.cost_per_day():.4f}"
    )

    if report.by_provider:
        lines.append("")
        lines.append(f"{bold}Spend by provider:{reset}")
        for b in report.by_provider[:5]:
            lines.append(
                f"  {green}${b.cost_usd:.4f}{reset}  "
                f"{b.provider}/{b.model} ({b.tier}) — {b.calls} calls"
            )

    if report.top_skills:
        lines.append("")
        lines.append(f"{bold}Top skills:{reset}")
        for s in report.top_skills:
            role_suffix = (
                f" {dim}[{s.role}]{reset}"
                if s.role and s.role != "unknown" else ""
            )
            lines.append(
                f"  {s.calls:>3}× {s.skill_name}{role_suffix}"
            )

    if report.skills_run == 0 and report.inference_calls == 0:
        lines.append("")
        lines.append(
            f"{dim}No activity in the last {report.window_days}d. "
            f"Open the chat and ask the cofounder to do something.{reset}"
        )

    return "\n".join(lines)
