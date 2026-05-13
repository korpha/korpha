"""Stuck-work classification.

Three signal kinds today, mapped to common failure modes:

  * IDLE_IN_PROGRESS — a card sat in IN_PROGRESS past the idle
    threshold without progress. Usually means the director got
    stuck (LLM loop, missing tool, network failure).
  * REVIEW_OVERDUE — a card sat in REVIEW past the founder-review
    threshold. Usually means Mike is the bottleneck (digest got
    buried, Mike on vacation). Surfacing this beats letting the
    work age silently.
  * REWORK_LOOP — a card bounced REVIEW → IN_PROGRESS more than
    N times. The agent keeps shipping the same flaw; founder
    needs to either kill the card or rewrite the acceptance
    criteria.

Thresholds are configurable via ``default_thresholds()`` so a
chatty solo dev with a 5-min Ralph loop can tighten them, and
a slow side-hustle owner can loosen them. Defaults target Mike's
profile: 5–10 hr/week founder, side-hustle pace, daily check-ins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from korpha.kanban.model import (
    KanbanCard,
    KanbanCardEvent,
    KanbanColumn,
)


class StuckKind(StrEnum):
    """Why a card is flagged."""

    IDLE_IN_PROGRESS = "idle_in_progress"
    REVIEW_OVERDUE = "review_overdue"
    REWORK_LOOP = "rework_loop"


@dataclass(frozen=True)
class StuckSignal:
    """One stuck-work observation. Read-only — the classifier
    doesn't act, the caller decides whether to notify, escalate,
    or auto-archive."""

    kind: StuckKind
    card_id: UUID
    title: str
    column: KanbanColumn
    age_hours: float
    """How long the card's been in its current state."""

    summary: str
    """One-sentence human-readable explanation. Goes straight
    into digests / dashboard tooltips."""

    severity: str = "warning"
    """``warning`` (yellow) / ``critical`` (red). Critical fires
    once we're 3x past the threshold or have a full-blown rework
    loop."""

    detected_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )

    extra: dict = field(default_factory=dict)
    """Per-kind extra fields — bounce_count for REWORK_LOOP, etc."""


@dataclass(frozen=True)
class Thresholds:
    """How long is too long? Tunable per founder via
    ``classify_kanban_signals(thresholds=...)``."""

    idle_in_progress_hours: float = 6.0
    """Flag IN_PROGRESS cards that haven't moved in N hours.
    Default 6h — covers a normal work session; longer than that
    and the agent is probably wedged."""

    review_overdue_hours: float = 48.0
    """Flag REVIEW cards waiting for the founder past N hours.
    Default 48h — gives Mike a weekend's grace before pinging."""

    rework_bounce_threshold: int = 2
    """Flag a REWORK_LOOP after this many REVIEW → IN_PROGRESS
    kickbacks. 2 means "second bounce wakes the alarm" —
    the third would be wasted work."""

    critical_multiplier: float = 3.0
    """Severity escalates from warning → critical at threshold ×
    this multiplier (or after one extra rework bounce)."""


def default_thresholds() -> Thresholds:
    return Thresholds()


def _hours_since(when: datetime, now: datetime) -> float:
    # SQLite returns naive datetimes; normalize both sides to
    # UTC-aware so the subtraction doesn't crash on mixed TZ.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = now - when
    return max(0.0, delta.total_seconds() / 3600.0)


def _idle_signals(
    session: Session,
    business_id: UUID,
    *,
    now: datetime,
    thresholds: Thresholds,
) -> list[StuckSignal]:
    """Cards in IN_PROGRESS that haven't moved past the idle
    threshold."""
    cutoff = now - timedelta(hours=thresholds.idle_in_progress_hours)
    rows = list(session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == business_id)
        .where(KanbanCard.column == KanbanColumn.IN_PROGRESS)
        .where(KanbanCard.moved_at < cutoff)
    ).all())
    out: list[StuckSignal] = []
    for card in rows:
        age = _hours_since(card.moved_at, now)
        severity = (
            "critical"
            if age >= thresholds.idle_in_progress_hours * thresholds.critical_multiplier
            else "warning"
        )
        owner = (
            f"({card.owner_role.upper()})"
            if card.owner_role else ""
        )
        out.append(StuckSignal(
            kind=StuckKind.IDLE_IN_PROGRESS,
            card_id=card.id,
            title=card.title,
            column=card.column,
            age_hours=round(age, 1),
            severity=severity,
            summary=(
                f"{card.title} {owner} has been claimed for "
                f"{age:.1f}h with no progress — director may be "
                "wedged or LLM looping."
            ).strip(),
            detected_at=now,
            extra={
                "owner_role": card.owner_role,
                "claimed_at": (
                    card.claimed_at.isoformat()
                    if card.claimed_at else None
                ),
            },
        ))
    return out


def _review_overdue_signals(
    session: Session,
    business_id: UUID,
    *,
    now: datetime,
    thresholds: Thresholds,
) -> list[StuckSignal]:
    """Cards in REVIEW past the founder-review threshold."""
    cutoff = now - timedelta(hours=thresholds.review_overdue_hours)
    rows = list(session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == business_id)
        .where(KanbanCard.column == KanbanColumn.REVIEW)
        .where(KanbanCard.moved_at < cutoff)
    ).all())
    out: list[StuckSignal] = []
    for card in rows:
        age = _hours_since(card.moved_at, now)
        severity = (
            "critical"
            if age >= thresholds.review_overdue_hours * thresholds.critical_multiplier
            else "warning"
        )
        out.append(StuckSignal(
            kind=StuckKind.REVIEW_OVERDUE,
            card_id=card.id,
            title=card.title,
            column=card.column,
            age_hours=round(age, 1),
            severity=severity,
            summary=(
                f"{card.title} has been awaiting your review for "
                f"{age:.1f}h. Verify the evidence or kick it back "
                "for rework."
            ),
            detected_at=now,
            extra={
                "evidence_present": bool(card.review_evidence),
            },
        ))
    return out


def _rework_loop_signals(
    session: Session,
    business_id: UUID,
    *,
    now: datetime,
    thresholds: Thresholds,
) -> list[StuckSignal]:
    """Cards that bounced REVIEW → IN_PROGRESS more than the
    bounce threshold. Detection: count ``move`` events whose
    transition is REVIEW → IN_PROGRESS."""
    # Pull every ``move`` event and partition by card. SQLAlchemy's
    # window functions vary by dialect, so we count in Python — a
    # founder's board has tens of cards, not millions.
    events = list(session.exec(
        select(KanbanCardEvent)
        .where(KanbanCardEvent.business_id == business_id)
        .where(KanbanCardEvent.kind == "move")
        .where(KanbanCardEvent.from_column == KanbanColumn.REVIEW)
        .where(KanbanCardEvent.to_column == KanbanColumn.IN_PROGRESS)
    ).all())
    bounces_by_card: dict[UUID, int] = {}
    for ev in events:
        bounces_by_card[ev.card_id] = (
            bounces_by_card.get(ev.card_id, 0) + 1
        )

    out: list[StuckSignal] = []
    for card_id, count in bounces_by_card.items():
        if count < thresholds.rework_bounce_threshold:
            continue
        card = session.get(KanbanCard, card_id)
        if (
            card is None
            or card.business_id != business_id
            or card.column == KanbanColumn.ARCHIVED
        ):
            continue
        severity = (
            "critical"
            if count >= thresholds.rework_bounce_threshold + 1
            else "warning"
        )
        out.append(StuckSignal(
            kind=StuckKind.REWORK_LOOP,
            card_id=card.id,
            title=card.title,
            column=card.column,
            age_hours=round(_hours_since(card.moved_at, now), 1),
            severity=severity,
            summary=(
                f"{card.title} has been kicked back from REVIEW "
                f"{count}× — the agent keeps shipping the same "
                "flaw. Tighten the acceptance criteria or kill the "
                "card."
            ),
            detected_at=now,
            extra={"bounce_count": count},
        ))
    return out


def classify_kanban_signals(
    session: Session,
    business_id: UUID,
    *,
    now: Optional[datetime] = None,
    thresholds: Thresholds | None = None,
) -> list[StuckSignal]:
    """Top-level entry point — classify every stuck-work pattern
    on the board. Multi-tenant safe (every query filters by
    business_id). Sorted critical first, then by age desc so the
    digest's first line is the worst offender."""
    now = now or datetime.now(tz=timezone.utc)
    thresholds = thresholds or default_thresholds()

    signals: list[StuckSignal] = []
    signals += _idle_signals(
        session, business_id, now=now, thresholds=thresholds,
    )
    signals += _review_overdue_signals(
        session, business_id, now=now, thresholds=thresholds,
    )
    signals += _rework_loop_signals(
        session, business_id, now=now, thresholds=thresholds,
    )
    signals.sort(key=lambda s: (
        0 if s.severity == "critical" else 1,
        -s.age_hours,
    ))
    return signals
