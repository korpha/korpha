"""Post-pick-niche skill chain.

When the Founder picks a niche, we run a small fan-out of skills that
produce concrete deliverables (validation score, landing copy, cold-
email drafts, Stripe payment link) and persist each as a pending
Approval. The dashboard then surfaces these as "things waiting for
you", which is what the BRIEF.md 5-minute demo promises at the 4:30 /
5:00 marks ("landing live, prospects drafted, Stripe armed").

Called from the pick-niche HTTP handler via FastAPI BackgroundTasks
(immediate fire-and-forget). Same shape would slot into a heartbeat
handler later if we want the chain to retry / replay, but for a one-
off "do this now" trigger background tasks are simpler.

Per-skill errors are caught and logged; one failed skill should not
block the others — the Founder still benefits from the partial result.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from korpha.approvals.model import ActionClass, Approval, ApprovalStatus
from korpha.audit.model import Activity, ActorType
from korpha.business.model import Business
from korpha.kanban.model import CardPriority, KanbanCard, KanbanColumn
from korpha.cofounder.hiring import HiringService
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.skills import (
    SkillContext,
    SkillError,
)
from korpha.skills import (
    default_registry as skills_registry,
)

logger = logging.getLogger(__name__)


async def run_post_pick_niche_chain(
    *,
    engine: Engine,
    business_id: UUID,
    niche: dict[str, Any],
    cost_tracker_factory: Callable[[], CostTracker],
    line_kind: str | None = None,
) -> dict[str, Any]:
    """Run the post-pick chain. Returns a small report dict for tests
    (counts of approvals created + per-skill errors). Always opens its
    own session — caller's session is request-scoped and may be closed
    by the time we run.

    ``niche`` should be the picked candidate as returned by the niche
    skill: at minimum ``name``; ideally also ``value_prop`` and
    ``target_avatar`` so the downstream skills have real inputs.

    ``line_kind`` (PR-INT-4) optionally spawns a BusinessUnit Line at
    chain start. One of: pod | kdp | info | saas | affiliate | agency.
    None or 'default' = skip line creation (back-compat for single-CEO
    installs). The report dict gets ``business_unit_id`` populated
    when a line is created."""
    name = str(niche.get("name") or "").strip()
    if not name:
        return {"approvals_created": 0, "errors": ["empty niche name"]}

    avatar = str(niche.get("target_avatar") or "").strip() or "(unspecified)"
    value_prop = str(niche.get("value_prop") or "").strip() or "(unspecified)"

    approvals_created = 0
    errors: list[str] = []

    with Session(engine) as session:
        business = session.exec(
            select(Business).where(Business.id == business_id)
        ).first()
        if business is None:
            return {"approvals_created": 0, "errors": ["business not found"]}
        founder = session.exec(
            select(Founder).where(Founder.id == business.founder_id)
        ).first()
        if founder is None:
            return {"approvals_created": 0, "errors": ["founder not found"]}

        ceo = HiringService(session).ensure_ceo(business.id)
        try:
            tracker = cost_tracker_factory()
        except Exception as exc:
            # No provider configured / tracker setup failed. Don't crash
            # the bg task — the Founder still sees a usable dashboard,
            # just without the auto-drafted approvals.
            logger.warning("post-pick-niche chain: tracker factory failed: %s", exc)
            return {
                "approvals_created": 0,
                "errors": [f"tracker setup failed: {exc}"],
            }
        # PR-INT-4: optionally spawn a BusinessUnit Line first.
        # When line_kind is set + non-default, create the Line + hire
        # its Line VP; subsequent chain steps run in that unit's
        # context (SkillContext.business_unit_id), so cards / costs /
        # approvals all attribute to the line, not the legacy default.
        spawned_unit_id: UUID | None = None
        if line_kind and line_kind.lower() not in {"default", "none", ""}:
            try:
                spawned_unit_id = await _spawn_line_unit(
                    session=session,
                    business=business,
                    founder=founder,
                    tracker=tracker,
                    ceo_id=ceo.id,
                    line_kind=line_kind.lower(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "post-pick-niche chain: line spawn failed: %s",
                    exc, exc_info=True,
                )
                errors.append(f"line_spawn: {exc}")

        ctx = SkillContext(
            business=business,
            founder=founder,
            session=session,
            cost_tracker=tracker,
            invoking_agent_role_id=ceo.id,
            business_unit_id=spawned_unit_id,
        )

        # 1. Reality check on the picked niche. The validator scores
        #    market dynamics (demand, willingness-to-pay, distribution)
        #    independent of the founder fit the niche skill already
        #    judged — so a "kill" verdict here means market risk, NOT
        #    "your cofounder thinks this is bad" (the CEO just
        #    recommended it). The proposal is framed as a reality check
        #    + cheapest kill-test, not as a contradiction of the pick.
        try:
            r = await skills_registry.run(
                "validate.score_idea",
                ctx=ctx,
                args={"idea": name, "avatar": avatar},
            )
            verdict = (r.payload or {}).get("verdict") or "review"
            kill_test = (r.payload or {}).get("kill_test") or ""
            improvement = (r.payload or {}).get("improvement_path") or ""
            framed = (
                f"Reality check on {name!r}: cheapest test to know "
                f"if it has legs — {kill_test[:160]}"
                if kill_test
                else f"Reality check on {name!r} — review market notes"
            )
            session.add(
                Approval(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    agent_role_id=ceo.id,
                    action_class=ActionClass.INTERNAL,
                    proposal_summary=framed,
                    action_payload={
                        "kind": "reality_check",
                        "niche_name": name,
                        "market_verdict": verdict,
                        "kill_test": kill_test,
                        "improvement_path": improvement,
                        "result": r.payload,
                    },
                    status=ApprovalStatus.PENDING,
                )
            )
            session.add(
                Activity(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    actor_type=ActorType.AGENT,
                    actor_id=ceo.id,
                    event_type="onboard.reality_check",
                    payload={
                        "niche_name": name,
                        "market_verdict": verdict,
                    },
                )
            )
            approvals_created += 1
        except (SkillError, Exception) as exc:
            logger.warning("validate.score_idea failed in chain: %s", exc)
            errors.append(f"validate: {exc}")
            session.add(
                Approval(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    agent_role_id=ceo.id,
                    action_class=ActionClass.INTERNAL,
                    proposal_summary=(
                        f"Reality check on {name!r} — generation failed, "
                        "click Retry to regenerate"
                    ),
                    action_payload={
                        "kind": "reality_check_failed",
                        "niche_name": name,
                        "dispatch_error": str(exc)[:300],
                    },
                    status=ApprovalStatus.PENDING,
                )
            )
            approvals_created += 1

        # 2. Landing copy. Real deliverable — Founder will skim and
        #    approve / tweak.
        try:
            r = await skills_registry.run(
                "landing.draft_copy",
                ctx=ctx,
                args={
                    "audience": avatar,
                    "value_prop": value_prop,
                    "stage": "waitlist",
                },
            )
            session.add(
                Approval(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    agent_role_id=ceo.id,
                    action_class=ActionClass.PUBLIC_POST,
                    proposal_summary=(
                        f"Landing copy for {name!r} — review headline + CTA"
                    ),
                    action_payload={
                        "kind": "landing_copy",
                        "niche_name": name,
                        "result": r.payload,
                    },
                    status=ApprovalStatus.PENDING,
                )
            )
            session.add(
                Activity(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    actor_type=ActorType.AGENT,
                    actor_id=ceo.id,
                    event_type="onboard.landing_drafted",
                    payload={"niche_name": name},
                )
            )
            approvals_created += 1
        except (SkillError, Exception) as exc:
            logger.warning("landing.draft_copy failed in chain: %s", exc)
            errors.append(f"landing: {exc}")
            session.add(
                Approval(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    agent_role_id=ceo.id,
                    action_class=ActionClass.PUBLIC_POST,
                    proposal_summary=(
                        f"Landing copy for {name!r} — generation failed, "
                        "click Retry to regenerate"
                    ),
                    action_payload={
                        "kind": "landing_copy_failed",
                        "niche_name": name,
                        "dispatch_error": str(exc)[:300],
                    },
                    status=ApprovalStatus.PENDING,
                )
            )
            approvals_created += 1

        # 3a. Stripe payment link draft. The skill itself adds an
        #     Approval (action_class=COMMERCE) — Founder approves to
        #     actually call Stripe. Lands the BRIEF "Stripe armed" beat.
        #     Skipped silently if we can't parse a price out of the
        #     niche's price_band (e.g. "$29-99/mo" → $29).
        price_band = str(niche.get("price_band") or "").strip()
        amount_usd = _parse_price_lower_bound(price_band)
        if amount_usd is not None:
            try:
                await skills_registry.run(
                    "commerce.create_payment_link",
                    ctx=ctx,
                    args={
                        "name": name,
                        "amount_usd": amount_usd,
                        "description": value_prop if value_prop != "(unspecified)" else None,
                    },
                )
                approvals_created += 1
            except (SkillError, Exception) as exc:
                logger.warning("commerce.create_payment_link failed in chain: %s", exc)
                errors.append(f"commerce: {exc}")

        # 3. First-week kanban cards — real tasks Mike acts on this
        #    week, not theater. We don't auto-generate cold emails
        #    (no contact list yet — drafts to nobody are useless) and
        #    we don't auto-generate a kickoff meeting with the AI
        #    cofounder (you don't meet with software). Instead: two
        #    actionable cards land in BACKLOG so the Line VP has
        #    something to claim on Day 1.
        try:
            session.add(
                KanbanCard(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    title=f"Publish landing for {name!r} + share URL back",
                    body=(
                        f"Take the approved landing copy and put it on a "
                        f"real page (Carrd, Framer, or your existing site). "
                        f"Drop the URL in chat once it's live so the "
                        f"cofounder can wire the Stripe link into it and "
                        f"start tracking signups."
                    ),
                    column=KanbanColumn.BACKLOG,
                    priority=CardPriority.HIGH,
                    acceptance_criteria=[
                        "Landing page is live at a real URL",
                        "URL pasted into chat",
                    ],
                    owner_role="founder",
                )
            )
            experiment = str(niche.get("validation_experiment") or "").strip()
            if not experiment:
                experiment = (
                    "Find 10 people in the target avatar, talk to them, "
                    "learn what they actually want to pay for."
                )
            session.add(
                KanbanCard(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    title=(
                        f"Get 10 real conversations with {avatar[:60]}"
                    ),
                    body=(
                        f"Validation experiment for {name!r}: {experiment} "
                        f"Drop notes from each conversation into chat so the "
                        f"cofounder can spot patterns + sharpen the offer."
                    ),
                    column=KanbanColumn.BACKLOG,
                    priority=CardPriority.HIGH,
                    acceptance_criteria=[
                        "10 conversations logged",
                        "Patterns summarized back to cofounder",
                    ],
                    owner_role="founder",
                )
            )
            session.add(
                Activity(
                    business_id=business.id,
                    business_unit_id=spawned_unit_id,
                    actor_type=ActorType.AGENT,
                    actor_id=ceo.id,
                    event_type="onboard.first_week_cards_created",
                    payload={"niche_name": name, "count": 2},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "first-week kanban cards failed in chain: %s", exc,
            )
            errors.append(f"kanban: {exc}")

        session.commit()

    return {"approvals_created": approvals_created, "errors": errors}


async def _spawn_line_unit(
    *,
    session,
    business,
    founder,
    tracker,
    ceo_id: UUID,
    line_kind: str,
) -> UUID:
    """PR-INT-4: spawn a BusinessUnit Line as part of onboarding.

    Uses ``hr.start_business_line`` directly so the Line VP gets
    hired + the line pack defaults applied identically to a chat-
    driven spawn. Returns the new unit's id.

    If no default unit exists yet (single-CEO install without PR2
    backfill), create one transparently so the LINE has a parent to
    nest under.
    """
    from korpha.business_units.board import BusinessUnitBoard
    from korpha.business_units.model import BusinessUnitKind
    from korpha.line_packs import default_registry as line_pack_registry
    from korpha.skills import (
        SkillContext as _SC, default_registry as _skills,
    )

    board = BusinessUnitBoard(session)
    units = board.list_for_business(business.id, include_archived=False)
    default_unit = next(
        (u for u in units
         if u.parent_id is None and u.kind == BusinessUnitKind.DEFAULT),
        None,
    )
    if default_unit is None:
        default_unit = board.create(
            business_id=business.id, name=business.name,
            kind=BusinessUnitKind.DEFAULT,
        )

    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=tracker, invoking_agent_role_id=ceo_id,
    )
    result = await _skills.run(
        "hr.start_business_line",
        ctx=ctx,
        args={"kind": line_kind, "parent_unit_id": str(default_unit.id)},
    )
    new_unit_id = UUID(result.payload["unit_id"])

    # Install the matching Line Pack defaults onto the unit.
    pack = next(
        (p for p in line_pack_registry.all() if p.line_kind == line_kind),
        None,
    )
    if pack is not None:
        new_unit = board.get(new_unit_id)
        if new_unit is not None:
            pack.setup_unit(session, new_unit)

    logger.info(
        "onboarding: spawned %s line unit %s for business %s",
        line_kind, new_unit_id, business.id,
    )
    return new_unit_id


def _parse_price_lower_bound(price_band: str) -> float | None:
    """Pull the lowest dollar number out of a price-band string.

    Niche skill emits formats like ``"$29-99/mo"``, ``"$99/mo"``,
    ``"$29-49"``. We pick the lowest number that looks like a price
    so the first Stripe link is conservative — Founder can always
    create a higher-priced one later.

    Returns None when the string has no recognizable number, which
    causes the chain to skip the Stripe step rather than guess.
    """
    if not price_band:
        return None
    matches = re.findall(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", price_band)
    if not matches:
        return None
    try:
        nums = [float(m) for m in matches]
    except ValueError:
        return None
    nums = [n for n in nums if n > 0]
    if not nums:
        return None
    return min(nums)


__all__ = ["run_post_pick_niche_chain"]
