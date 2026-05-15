"""Structured fault rules over the kanban board.

Port of Hermes's ``hermes_cli/kanban_diagnostics.py``. Stateless,
read-only rules that walk a card + its events + dispatch metadata
and emit ``Diagnostic`` records the dashboard and CLI can surface.

Rules only flag **operator-fixable** situations — phantom card-ids
the CEO cited, crash loops, stuck-blocked tasks. Provider hiccups
("503 on this attempt") are NOT diagnostics; they're transient and
the dispatcher will retry.

Each diagnostic carries actions the dashboard can render as
buttons (e.g. "Reassign to CMO", "Unblock", "Archive") and the CLI
can list as hints.

Auto-clearing: when the underlying failure resolves (card moves
to DONE, claim succeeds, blocker note removed), the rule stops
firing. Stateless evaluation means no extra DB writes; callers
re-run rules on-demand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from korpha.kanban.model import KanbanCard

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    """Severity rungs, least → most urgent. UI colors them amber /
    orange / red. Sorted outputs put critical first so operators
    see worst fires at the top."""

    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True)
class DiagnosticAction:
    """A structured action a UI can render as a button or CLI hint."""

    label: str
    """Display label, e.g. 'Unblock card', 'Reassign to CMO'."""

    action_kind: str
    """Canonical action identifier, e.g. 'unblock', 'archive',
    'reassign', 'reset_failure_count'."""

    args: dict[str, str] = field(default_factory=dict)
    """Structured args the dashboard/CLI passes to the executor."""


@dataclass(frozen=True)
class Diagnostic:
    """A single fault signal. Stateless — emitted by rules,
    consumed by UIs."""

    kind: str
    """Canonical code; tests + UI match on this. Examples:
    'stuck_blocked', 'spawn_crash_loop', 'stale_claim_reclaimed',
    'card_no_owner', 'card_failed_repeatedly'."""

    severity: Severity
    title: str
    """One-line human summary."""

    detail: str
    """Longer text for tooltip / detail panel."""

    card_id: str
    """Target card's UUID string."""

    actions: tuple[DiagnosticAction, ...] = ()


# How long a BLOCKED card waiting on operator attention is "stuck".
_STUCK_BLOCKED_HOURS = 24

# Failures count from dispatcher metadata before we flag a card as
# crash-looping. Match Hermes's failure_limit default minus 1 (so
# the warning fires before auto-block).
_CRASH_LOOP_FAILURES = 2

# A claim sitting at the TTL boundary suggests the executor isn't
# heart-beating. Flag if claimed_at is within 90% of TTL with no
# review_evidence — gives operators a heads-up before auto-reclaim.
_NEAR_TTL_FRACTION = 0.9


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate_card(
    session: "Session",
    card: "KanbanCard",
    *,
    claim_ttl_seconds: int = 900,
) -> list[Diagnostic]:
    """Run every rule against a single card. Returns the list of
    Diagnostics that fire. Stateless — no DB writes.

    Pass through to ``evaluate_board`` when you want a per-board
    sweep."""
    from korpha.kanban.model import KanbanColumn

    out: list[Diagnostic] = []

    # -- Stuck in BLOCKED for too long ---------------------------------
    if card.column == KanbanColumn.BLOCKED:
        moved = _aware(card.moved_at) or _aware(card.updated_at)
        if moved is not None:
            age = _utc_now() - moved
            if age > timedelta(hours=_STUCK_BLOCKED_HOURS):
                out.append(Diagnostic(
                    kind="stuck_blocked",
                    severity=(
                        Severity.ERROR if age <= timedelta(days=3)
                        else Severity.CRITICAL
                    ),
                    title=(
                        f"Card blocked for "
                        f"{int(age.total_seconds() / 3600)}h"
                    ),
                    detail=(
                        f"`{card.title[:80]}` has been BLOCKED with "
                        "no movement. Operator attention required."
                    ),
                    card_id=str(card.id),
                    actions=(
                        DiagnosticAction(
                            label="Unblock → BACKLOG",
                            action_kind="unblock",
                            args={"target": "backlog"},
                        ),
                        DiagnosticAction(
                            label="Archive",
                            action_kind="archive",
                        ),
                    ),
                ))

    # -- Crash loop (failure count high) -------------------------------
    meta = card.metadata_json or {}
    failures = int(meta.get("_dispatch_failure_count", 0))
    if failures >= _CRASH_LOOP_FAILURES and card.column in (
        KanbanColumn.READY, KanbanColumn.IN_PROGRESS,
    ):
        last = meta.get("_dispatch_last_failure", "(no detail)")
        out.append(Diagnostic(
            kind="spawn_crash_loop",
            severity=(
                Severity.ERROR if failures < 3 else Severity.CRITICAL
            ),
            title=(
                f"Card has failed dispatch {failures} time(s)"
            ),
            detail=(
                f"`{card.title[:80]}` keeps crashing. Last failure: "
                f"{str(last)[:200]}"
            ),
            card_id=str(card.id),
            actions=(
                DiagnosticAction(
                    label="Reset failure count + retry",
                    action_kind="reset_failure_count",
                ),
                DiagnosticAction(
                    label="Block manually",
                    action_kind="block",
                ),
                DiagnosticAction(
                    label="Reassign to different role",
                    action_kind="reassign",
                ),
            ),
        ))

    # -- Card in SPECIFY / IN_PROGRESS with no owner_role --------------
    if (
        card.column in (KanbanColumn.SPECIFY, KanbanColumn.READY)
        and not card.owner_role
    ):
        out.append(Diagnostic(
            kind="card_no_owner",
            severity=Severity.WARNING,
            title="Card has no c-suite owner",
            detail=(
                f"`{card.title[:80]}` is in {card.column.value} "
                "without owner_role set. The dispatcher won't know "
                "who to route the work to."
            ),
            card_id=str(card.id),
            actions=(
                DiagnosticAction(
                    label="Assign to CMO",
                    action_kind="reassign",
                    args={"new_owner_role": "cmo"},
                ),
                DiagnosticAction(
                    label="Assign to CTO",
                    action_kind="reassign",
                    args={"new_owner_role": "cto"},
                ),
                DiagnosticAction(
                    label="Assign to COO",
                    action_kind="reassign",
                    args={"new_owner_role": "coo"},
                ),
            ),
        ))

    # -- Claim near TTL — executor not heart-beating -------------------
    if (
        card.column == KanbanColumn.IN_PROGRESS
        and card.claimed_at is not None
    ):
        claimed = _aware(card.claimed_at)
        if claimed is not None:
            age = (_utc_now() - claimed).total_seconds()
            if age > claim_ttl_seconds * _NEAR_TTL_FRACTION:
                out.append(Diagnostic(
                    kind="claim_near_ttl",
                    severity=Severity.WARNING,
                    title="Claim near TTL — executor may have hung",
                    detail=(
                        f"`{card.title[:80]}` claim is "
                        f"{int(age)}s old (TTL "
                        f"{claim_ttl_seconds}s). Executor should "
                        "call kanban.heartbeat or it will be "
                        "reclaimed."
                    ),
                    card_id=str(card.id),
                    actions=(
                        DiagnosticAction(
                            label="Force reclaim now",
                            action_kind="force_reclaim",
                        ),
                    ),
                ))

    # -- IN_PROGRESS with no acceptance criteria — can't verify --------
    if (
        card.column == KanbanColumn.IN_PROGRESS
        and not card.acceptance_criteria
    ):
        out.append(Diagnostic(
            kind="missing_acceptance_criteria",
            severity=Severity.WARNING,
            title="No acceptance criteria — REVIEW will be blind",
            detail=(
                f"`{card.title[:80]}` is being worked on without "
                "acceptance criteria. The REVIEW gate has nothing "
                "to verify against."
            ),
            card_id=str(card.id),
            actions=(
                DiagnosticAction(
                    label="Specify acceptance criteria",
                    action_kind="specify_card",
                ),
            ),
        ))

    return out


def evaluate_board(
    *,
    engine: "Engine",
    business_id: str | None = None,
    claim_ttl_seconds: int = 900,
) -> list[Diagnostic]:
    """Run every rule against every non-DONE non-ARCHIVED card on
    the board. Returns all firing Diagnostics, sorted critical
    first. Stateless — no DB writes."""
    from uuid import UUID

    from sqlmodel import Session, select

    from korpha.kanban.model import KanbanCard, KanbanColumn

    out: list[Diagnostic] = []
    with Session(engine) as session:
        stmt = (
            select(KanbanCard)
            .where(KanbanCard.column.not_in([  # type: ignore[union-attr]
                KanbanColumn.DONE,
                KanbanColumn.ARCHIVED,
            ]))
        )
        if business_id:
            try:
                bid = UUID(business_id)
                stmt = stmt.where(KanbanCard.business_id == bid)
            except (ValueError, TypeError):
                pass
        for card in session.exec(stmt).all():
            out.extend(evaluate_card(
                session, card,
                claim_ttl_seconds=claim_ttl_seconds,
            ))

    # Critical first → error → warning. Within each severity, by
    # kind so stable display order.
    severity_rank = {
        Severity.CRITICAL: 0,
        Severity.ERROR: 1,
        Severity.WARNING: 2,
    }
    out.sort(key=lambda d: (severity_rank[d.severity], d.kind, d.card_id))
    return out


__all__ = [
    "Diagnostic",
    "DiagnosticAction",
    "Severity",
    "evaluate_board",
    "evaluate_card",
]
