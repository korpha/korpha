"""Bridge that calls ``Workforce.dispatch()`` on kanban cards in
IN_PROGRESS. Without this bridge, cards land in IN_PROGRESS via
``kanban.fire_sprint`` (or manual claim) but never produce real
output — the system looks busy but ships nothing.

Three trigger modes share this code:

  1. Inline   — ``kanban.fire_sprint`` calls it immediately after
                claiming cards. Fastest founder feedback.
  2. Cron     — a ScriptCron preset polls IN_PROGRESS every N min
                and dispatches any cards without recent activity.
                Catches cards that landed via routes other than
                fire_sprint (manual move, future skills, etc.).
  3. Hook     — a POST_SKILL_CALL plugin hook fires after
                ``kanban.fire_sprint`` results land. Decoupled
                from the skill code; opt-in via plugin config.

Which trigger runs is controlled by
``Settings.workforce_auto_dispatch_mode``:

  - ``"inline"`` (default): mode 1 only.
  - ``"cron"``:   mode 2 only (founder installs cron preset).
  - ``"hook"``:   mode 3 only (plugin hook handles it).
  - ``"all"``:    1 + 2 + 3 (idempotent — see below).
  - ``"off"``:    no automatic trigger; manual via /approvals/*.

Idempotency: the bridge filters to cards currently in IN_PROGRESS
that *haven't been dispatched in the last N minutes*. The
``moved_at`` column tracks the last column transition; a card that
just moved into IN_PROGRESS is fresh and dispatched once. A card
already IN_PROGRESS for >N minutes is treated as "still working"
unless force=True.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlmodel import Session

    from korpha.business.model import Business
    from korpha.cofounder.workforce import Workforce
    from korpha.identity.model import Founder
    from korpha.inference.cost_tracker import CostTracker

logger = logging.getLogger(__name__)


# Stamp metadata key so we can detect 're-dispatch' attempts and
# avoid running the same executor twice on the same card.
_DISPATCH_STAMP_KEY = "auto_dispatch_at"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _card_dispatch_eligible(card, *, stale_after_minutes: int) -> bool:
    """A card is eligible for auto-dispatch when:
      - It is currently in IN_PROGRESS.
      - It hasn't been auto-dispatched yet (no stamp).
      - OR its previous stamp is older than ``stale_after_minutes``
        and the card is still IN_PROGRESS (i.e. executor didn't
        finish and we want to retry).
    """
    try:
        from korpha.kanban.model import KanbanColumn
        if card.column != KanbanColumn.IN_PROGRESS:
            return False
    except Exception:  # noqa: BLE001
        return False
    meta = card.metadata_json or {}
    stamp_raw = meta.get(_DISPATCH_STAMP_KEY) if isinstance(meta, dict) else None
    if not stamp_raw:
        return True
    try:
        stamp = datetime.fromisoformat(str(stamp_raw))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    age = _utc_now() - stamp
    return age >= timedelta(minutes=stale_after_minutes)


def _mark_card_dispatched(session: "Session", card) -> None:
    """Stamp the card's metadata so subsequent triggers skip it."""
    meta = dict(card.metadata_json or {})
    meta[_DISPATCH_STAMP_KEY] = _utc_now().isoformat()
    card.metadata_json = meta
    session.add(card)
    session.commit()


def _build_workforce(
    session: "Session",
    cost_tracker: "CostTracker",
) -> "Workforce":
    """Construct a Workforce with the standard CTO/CMO/COO directors.
    Mirrors the wiring in ``_build_ceo`` in ``korpha/api/server.py``."""
    from korpha.approvals.gate import ApprovalGate
    from korpha.blockers.queue import BlockerQueue
    from korpha.cofounder.hiring import HiringService
    from korpha.cofounder.workforce import DirectorFactory, Workforce

    hiring = HiringService(session)
    gate = ApprovalGate(session)
    queue = BlockerQueue(session=session)
    factory = DirectorFactory(
        session=session, cost_tracker=cost_tracker,
        queue=queue, hiring=hiring,
    )
    return Workforce.with_default_directors(director_factory=factory)


def _format_task(card) -> str:
    """Render a kanban card as a workforce task string with the
    role tag the dispatcher expects.

    Workforce.select_executor parses ``[CTO]`` / ``[CMO]`` / ``[COO]``
    tags. We use the card's ``owner_role`` to pick the right one.
    """
    role = (card.owner_role or "").strip().lower()
    tag = role.upper() if role in {"cto", "cmo", "coo"} else ""
    title = card.title.strip()
    if tag:
        return f"[{tag}] {title}"
    return title


async def dispatch_pending_cards(
    *,
    business: "Business",
    founder: "Founder",
    session: "Session",
    cost_tracker: "CostTracker",
    card_ids: list[UUID] | None = None,
    stale_after_minutes: int = 30,
    max_cards: int = 12,
    force: bool = False,
) -> dict[str, object]:
    """Find IN_PROGRESS cards that haven't been auto-dispatched
    and run them through ``Workforce.dispatch()``.

    ``card_ids`` (optional) restricts the scan to specific cards
    (used by the inline trigger which knows exactly what just
    moved). Otherwise scans every IN_PROGRESS card for the business.

    Returns a small report dict for the caller to log / surface.
    """
    from sqlmodel import select

    from korpha.kanban.model import KanbanCard, KanbanColumn

    stmt = (
        select(KanbanCard)
        .where(KanbanCard.business_id == business.id)
        .where(KanbanCard.column == KanbanColumn.IN_PROGRESS)
    )
    if card_ids:
        stmt = stmt.where(KanbanCard.id.in_(card_ids))  # type: ignore[attr-defined]
    rows = list(session.exec(stmt).all())
    if not rows:
        return {
            "dispatched_count": 0, "skipped_count": 0,
            "tasks": [], "reason": "no_in_progress_cards",
        }

    eligible = []
    skipped: list[dict[str, str]] = []
    for c in rows:
        if force or _card_dispatch_eligible(
            c, stale_after_minutes=stale_after_minutes,
        ):
            eligible.append(c)
        else:
            skipped.append({
                "card_id": str(c.id),
                "title": c.title[:60],
                "reason": "already_dispatched_recently",
            })

    if not eligible:
        return {
            "dispatched_count": 0,
            "skipped_count": len(skipped),
            "skipped": skipped,
            "tasks": [],
            "reason": "all_dispatched_recently",
        }
    eligible = eligible[:max_cards]
    tasks = [_format_task(c) for c in eligible]

    workforce = _build_workforce(session, cost_tracker)
    logger.info(
        "auto_dispatch: firing %d task(s) for business=%s",
        len(tasks), business.id,
    )
    try:
        results = await workforce.dispatch(
            business=business, founder=founder, tasks=tasks,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto_dispatch: workforce.dispatch raised")
        return {
            "dispatched_count": 0,
            "skipped_count": len(skipped),
            "error": f"{type(exc).__name__}: {exc}",
            "tasks": tasks,
        }

    # Stamp every card we just dispatched so the next trigger
    # doesn't re-fire them.
    for c in eligible:
        try:
            _mark_card_dispatched(session, c)
        except Exception:  # noqa: BLE001
            logger.warning(
                "auto_dispatch: failed to stamp card %s",
                c.id, exc_info=True,
            )

    summary = {
        "dispatched_count": len(eligible),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "tasks": tasks,
        "results": [
            {
                "title": getattr(r, "title", "?")[:60],
                "status": getattr(r, "status", "?"),
                "summary": (
                    (getattr(r, "summary", "") or "")[:200]
                ),
            }
            for r in results
        ],
    }
    return summary


def auto_dispatch_mode() -> str:
    """Cached read of Settings.workforce_auto_dispatch_mode.
    Default ``"inline"``. Returns lowercase string."""
    from korpha.config import get_settings
    return (get_settings().workforce_auto_dispatch_mode or "inline").lower()


def register_post_skill_hook(*, force: bool = False) -> bool:
    """Path 3 of the workforce auto-dispatch triggers — register a
    ``POST_SKILL_CALL`` hook that fires ``dispatch_pending_cards``
    on cards listed in the result of ``kanban.fire_sprint``.

    Skipped by default (returns False) unless
    ``Settings.workforce_auto_dispatch_mode`` is ``"hook"`` or
    ``"all"``, or ``force=True``. Idempotent — registering twice
    is safe but only one listener is kept.
    """
    from korpha.plugins.hooks import (
        HookKind,
        PostSkillCallEvent,
        hook_registry,
    )

    if not force and auto_dispatch_mode() not in {"hook", "all"}:
        return False

    # If we're already registered, no-op.
    for name, _ in hook_registry.listeners(HookKind.POST_SKILL_CALL):
        if name == "_auto_dispatch_post_skill":
            return True

    async def _on_post_skill_call(evt: PostSkillCallEvent) -> None:
        if not evt.succeeded or evt.skill_name != "kanban.fire_sprint":
            return
        payload = getattr(evt.result, "payload", None) or {}
        fired = payload.get("fired") or []
        if not fired:
            return
        card_ids: list[UUID] = []
        for f in fired:
            cid = f.get("card_id") if isinstance(f, dict) else None
            if cid:
                try:
                    card_ids.append(UUID(cid))
                except (ValueError, TypeError):
                    pass
        if not card_ids:
            return
        # Plugin hooks fire fire-and-forget without their own
        # session/cost_tracker. We rely on the global setting and
        # rebuild from the API server's factories. This makes the
        # hook self-contained but slightly heavier per call.
        try:
            from korpha.api.server import _build_pool_pieces
            from korpha.business.model import Business as _Business
            from korpha.db._session import get_engine
            from korpha.identity.model import Founder as _Founder
            from korpha.inference import InferencePool as _Pool
            from korpha.inference.cost_tracker import (
                CostTracker as _Tracker,
            )
            from sqlmodel import Session as _Session, select as _select

            providers_list, accounts_list = _build_pool_pieces()
            if not accounts_list:
                return
            pool = _Pool(
                providers=providers_list, accounts=accounts_list,
            )
            tracker = _Tracker(pool=pool)
            with _Session(get_engine()) as session:
                business = session.exec(_select(_Business)).first()
                founder = session.exec(_select(_Founder)).first()
                if business is None or founder is None:
                    return
                await dispatch_pending_cards(
                    business=business, founder=founder,
                    session=session, cost_tracker=tracker,
                    card_ids=card_ids,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "auto_dispatch hook: dispatch_pending_cards raised"
            )

    hook_registry.register(
        HookKind.POST_SKILL_CALL, _on_post_skill_call,
        plugin_name="_auto_dispatch_post_skill",
    )
    return True


__all__ = [
    "auto_dispatch_mode",
    "dispatch_pending_cards",
    "register_post_skill_hook",
]
