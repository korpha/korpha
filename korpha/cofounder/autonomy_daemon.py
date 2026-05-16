"""Autonomy daemon — pulls BACKLOG cards into work without Mike typing "go".

The daemon is a single tick (:func:`run_tick`) wired up two ways:

  - **Cron preset** — ``aigenteur cron add-autonomy`` registers a
    recurring job that calls this every 15 min. That's the
    set-it-and-forget-it path Mike uses once he's flipped his
    Business into ``autonomy_mode != off``.

  - **One-shot** — ``aigenteur autonomy run-tick`` does exactly one
    pass for testing / triage. Lets a developer or Mike force a
    progression without waiting for cron.

Tick contract (per Business):

  1. Resolve mode + caps via :mod:`korpha.cofounder.autonomy`.
  2. If paused (mode_off / iterations_reached / *_budget_reached),
     log + skip.
  3. If IN_PROGRESS is non-empty, log "team busy, skipping" — we
     don't want to multiplex multiple Directors over a single
     business while one is still mid-attempt. (Concurrency lives at
     the BusinessUnit layer; this daemon is per-Business.)
  4. Pick up to ``batch_size`` BACKLOG cards (oldest-first within
     priority desc). Promote each through SPECIFY → READY →
     IN_PROGRESS, claiming to the Line VP. Identical to what
     ``kanban.fire_sprint`` does, but driven by the daemon picking
     IDs instead of the CEO supplying them.
  5. Call :func:`dispatch_pending_cards` on the just-fired IDs so
     the Workforce actually starts the work this tick.

What this daemon is NOT:

  - Not a fallback for stale claims — :mod:`korpha.kanban.diagnostics`
    already handles that.
  - Not a planner — it doesn't create new BACKLOG cards. When BACKLOG
    runs dry it just stops; Mike (or the CEO) plans the next sprint.
  - Not a per-iteration spend tracker — the BudgetPolicy enforcer
    already pauses on $ caps; we just respect that flag.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.business.model import AutonomyMode, Business
from korpha.cofounder.autonomy import (
    AutonomySnapshot,
    evaluate as evaluate_autonomy,
)
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.kanban.board import KanbanBoard, KanbanError
from korpha.kanban.model import CardPriority, KanbanCard, KanbanColumn

logger = logging.getLogger(__name__)


DEFAULT_BATCH_SIZE = 3
"""How many BACKLOG cards to fire per tick. Small batch keeps the
dispatcher observable and gives review-evidence time to land before
another wave goes out."""


@dataclass
class TickResult:
    """Compact report from one tick. The CLI and the cron preset
    log this; the dashboard pulls the most-recent one for status."""

    business_id: UUID
    fired_card_ids: list[UUID]
    skipped_reason: str | None
    """Set when nothing was fired and we want to tell the operator why.
    Examples: ``mode_off``, ``team_busy``, ``backlog_empty``,
    ``daily_budget_reached``."""
    dispatched_count: int
    snapshot: AutonomySnapshot

    @property
    def fired_count(self) -> int:
        return len(self.fired_card_ids)


# ---------------------------------------------------------------- internals


def _infer_owner_role(title: str, body: str) -> str:
    """Cheap keyword routing copy of the fire_sprint heuristic. We
    mirror it instead of importing because the daemon shouldn't pull
    in the whole skills module just for one helper. If the routing
    drifts, fix both call sites.

    Returns 'cmo' / 'cto' / 'coo' / 'ceo' — these are the canonical
    c-suite owner_role strings the Workforce knows how to dispatch."""
    text = f"{title} {body}".lower()
    if any(t in text for t in (
        "post", "social", "tweet", "blog", "newsletter", "ad ",
        "ads", "marketing", "outreach", "calendar", "campaign",
        "instagram", "tiktok", "youtube",
    )):
        return "cmo"
    if any(t in text for t in (
        "deploy", "ship", "build", "implement", "fix", "bug",
        "api", "backend", "frontend", "database",
    )):
        return "cto"
    if any(t in text for t in (
        "research", "validate", "analyze", "report", "review",
        "audit", "evaluate", "p&l", "finance", "budget",
    )):
        return "coo"
    return "ceo"


def _pick_backlog_cards(
    session: Session, *, business_id: UUID, batch_size: int,
) -> list[KanbanCard]:
    """Top-N BACKLOG cards ordered by priority desc, created_at asc.

    Priority order maps: HIGH > NORMAL > LOW. We CASE on the enum
    string so the ordering is stable across SQLite + Postgres without
    a real enum type."""
    rows = list(session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == business_id)
        .where(KanbanCard.column == KanbanColumn.BACKLOG)
        .order_by(KanbanCard.created_at)  # type: ignore[arg-type]
    ).all())
    priority_rank = {
        CardPriority.HIGH: 0,
        CardPriority.NORMAL: 1,
        CardPriority.LOW: 2,
    }
    rows.sort(key=lambda c: (
        priority_rank.get(c.priority, 1),
        c.created_at,
    ))
    return rows[:batch_size]


def _line_vp_for_unit(
    session: Session, *, unit_id: UUID | None,
) -> AgentRole | None:
    """First active WORKER (Line VP) for the unit. Cards without a
    unit fall back to the business-default Line VP if one exists."""
    if unit_id is None:
        return None
    rows = list(session.exec(
        select(AgentRole)
        .where(AgentRole.business_unit_id == unit_id)
        .where(AgentRole.role_type == RoleType.WORKER)
        .where(AgentRole.is_active.is_(True))  # type: ignore[attr-defined]
    ).all())
    return rows[0] if rows else None


def _promote_card(
    *,
    board: KanbanBoard,
    session: Session,
    card: KanbanCard,
) -> tuple[bool, str | None]:
    """BACKLOG → SPECIFY → READY → IN_PROGRESS (claim). Returns
    (success, error_message). Mirrors the fire_sprint promotion
    path but operates on one card at a time so the daemon can keep
    going past a single failure."""
    try:
        owner_role = card.owner_role or _infer_owner_role(
            card.title, card.body or "",
        )

        if card.column == KanbanColumn.BACKLOG:
            board.specify(
                card.id,
                acceptance_criteria=[
                    "Deliverable matches the card title.",
                    "Output uploaded to the workspace and linked in "
                    "review evidence.",
                ],
                owner_role=owner_role,
            )

        if card.column != KanbanColumn.READY:
            board.move(card.id, KanbanColumn.READY)

        vp = _line_vp_for_unit(session, unit_id=card.business_unit_id)
        if vp is None:
            return False, "no_line_vp_for_unit"
        board.claim(
            card.id, agent_role_id=vp.id, actor_role=owner_role,
        )
        return True, None
    except KanbanError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "autonomy_daemon: unexpected error promoting card %s",
            card.id,
        )
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- public API


async def run_tick(
    *,
    session: Session,
    business: Business,
    founder: Founder,
    cost_tracker: Any,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> TickResult:
    """One full pass of the daemon for one business. See module docstring."""
    snap = evaluate_autonomy(session, business=business)

    if snap.paused:
        # The autonomy snapshot already carries the precise reason
        # (mode_off / iterations_reached / *_budget_reached). Pass it
        # through unchanged so log readers + dashboards match.
        return TickResult(
            business_id=business.id,
            fired_card_ids=[],
            skipped_reason=snap.paused_reason or "paused",
            dispatched_count=0,
            snapshot=snap,
        )

    in_progress = session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == business.id)
        .where(KanbanCard.column == KanbanColumn.IN_PROGRESS)
    ).all()
    if in_progress:
        return TickResult(
            business_id=business.id,
            fired_card_ids=[],
            skipped_reason="team_busy",
            dispatched_count=0,
            snapshot=snap,
        )

    cards = _pick_backlog_cards(
        session, business_id=business.id, batch_size=batch_size,
    )
    if not cards:
        return TickResult(
            business_id=business.id,
            fired_card_ids=[],
            skipped_reason="backlog_empty",
            dispatched_count=0,
            snapshot=snap,
        )

    # ITERATIONS mode also caps within a single tick — if Mike set
    # daily_max_iterations=5 and we've already done 4, we only fire
    # one card. The mode==DAILY_BUDGET / MONTHLY_ONLY paths don't
    # cap the batch here; the BudgetPolicy enforcer trips between
    # cards if spend lands over the line mid-batch.
    if snap.mode == AutonomyMode.ITERATIONS and snap.iterations_cap is not None:
        remaining = max(0, snap.iterations_cap - snap.iterations_today)
        cards = cards[:remaining]
        if not cards:
            return TickResult(
                business_id=business.id,
                fired_card_ids=[],
                skipped_reason="iterations_reached",
                dispatched_count=0,
                snapshot=snap,
            )

    board = KanbanBoard(session)
    fired: list[UUID] = []
    for card in cards:
        ok, err = _promote_card(
            board=board, session=session, card=card,
        )
        if ok:
            fired.append(card.id)
        else:
            logger.warning(
                "autonomy_daemon: failed to promote card %s: %s",
                card.id, err,
            )

    if not fired:
        return TickResult(
            business_id=business.id,
            fired_card_ids=[],
            skipped_reason="all_promotions_failed",
            dispatched_count=0,
            snapshot=snap,
        )

    # Hand the freshly-fired cards to the existing executor. Importing
    # here (not at module top) keeps the daemon importable in tests
    # that mock out the workforce path.
    from korpha.cofounder.auto_dispatch import dispatch_pending_cards
    summary = await dispatch_pending_cards(
        business=business, founder=founder,
        session=session, cost_tracker=cost_tracker,
        card_ids=fired,
    )
    dispatched = int(summary.get("dispatched_count") or 0)

    logger.info(
        "autonomy_daemon: business=%s fired=%d dispatched=%d "
        "(mode=%s iter=%d/%s spend=%s/%s)",
        business.id, len(fired), dispatched,
        snap.mode.value,
        snap.iterations_today,
        snap.iterations_cap if snap.iterations_cap is not None else "-",
        snap.spent_today_usd,
        snap.daily_cap_usd if snap.daily_cap_usd is not None else "-",
    )

    return TickResult(
        business_id=business.id,
        fired_card_ids=fired,
        skipped_reason=None,
        dispatched_count=dispatched,
        snapshot=snap,
    )


async def run_tick_for_all_businesses(
    *,
    cost_tracker: Any,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[TickResult]:
    """Cron entry-point: tick every Business that has autonomy on.

    Builds its own Session so it can be called from a fire-and-forget
    cron job without the caller plumbing one in. Each Business gets a
    fresh session — failures on one don't poison the next."""
    from korpha.db._session import get_engine
    results: list[TickResult] = []

    with Session(get_engine()) as session:
        businesses = list(session.exec(
            select(Business)
            .where(Business.autonomy_mode.is_not(None))  # type: ignore[attr-defined]
            .where(Business.autonomy_mode != AutonomyMode.OFF)
            .where(Business.archived_at.is_(None))  # type: ignore[attr-defined]
        ).all())

    for biz in businesses:
        with Session(get_engine()) as session:
            biz_fresh = session.get(Business, biz.id)
            if biz_fresh is None:
                continue
            founder = session.get(Founder, biz_fresh.founder_id)
            if founder is None:
                continue
            try:
                tr = await run_tick(
                    session=session, business=biz_fresh,
                    founder=founder, cost_tracker=cost_tracker,
                    batch_size=batch_size,
                )
                results.append(tr)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "autonomy_daemon: tick raised for business=%s",
                    biz_fresh.id,
                )
    return results


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "TickResult",
    "run_tick",
    "run_tick_for_all_businesses",
]
