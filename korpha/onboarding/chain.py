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
from korpha.business.model import Business
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

        # 1. Validate the picked niche. This is read-only — captured as
        #    an Approval so the Founder sees the score in their queue.
        try:
            r = await skills_registry.run(
                "validate.score_idea",
                ctx=ctx,
                args={"idea": name, "avatar": avatar},
            )
            session.add(
                Approval(
                    business_id=business.id,
                    agent_role_id=ceo.id,
                    action_class=ActionClass.INTERNAL,
                    proposal_summary=(
                        f"Validation: {r.summary} for niche {name!r}"
                    ),
                    action_payload={
                        "kind": "validation_report",
                        "niche_name": name,
                        "result": r.payload,
                    },
                    status=ApprovalStatus.PENDING,
                )
            )
            approvals_created += 1
        except (SkillError, Exception) as exc:
            logger.warning("validate.score_idea failed in chain: %s", exc)
            errors.append(f"validate: {exc}")

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
            approvals_created += 1
        except (SkillError, Exception) as exc:
            logger.warning("landing.draft_copy failed in chain: %s", exc)
            errors.append(f"landing: {exc}")

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

        # 3. Cold-email opener variants. Founder picks one to send (or
        #    edits) — sending happens through the existing
        #    outreach.send_cold_email side-effect skill, separately gated.
        try:
            r = await skills_registry.run(
                "outreach.draft_cold_emails",
                ctx=ctx,
                args={
                    "avatar": avatar,
                    "value_prop": value_prop,
                    "channel": "email",
                },
            )
            session.add(
                Approval(
                    business_id=business.id,
                    agent_role_id=ceo.id,
                    action_class=ActionClass.EMAIL_OUTREACH,
                    proposal_summary=(
                        f"Cold-email drafts for {name!r} — pick one to send"
                    ),
                    action_payload={
                        "kind": "outreach_drafts",
                        "niche_name": name,
                        "result": r.payload,
                    },
                    status=ApprovalStatus.PENDING,
                )
            )
            approvals_created += 1
        except (SkillError, Exception) as exc:
            logger.warning("outreach.draft_cold_emails failed in chain: %s", exc)
            errors.append(f"outreach: {exc}")

        # 4. Kickoff invite — BRIEF.md minute 4:30 promise: "calendar
        #    slot for kickoff with cofounder tomorrow". Tomorrow at
        #    09:00 UTC, 30-min default. The .ics + add-link URLs land
        #    in the Approval payload so the dashboard renders both
        #    a download and a one-click "Add to Google Calendar" link.
        try:
            from datetime import datetime, timedelta, timezone

            tomorrow_9am = (
                datetime.now(timezone.utc).replace(
                    hour=9, minute=0, second=0, microsecond=0,
                ) + timedelta(days=1)
            )
            r = await skills_registry.run(
                "calendar.create_event",
                ctx=ctx,
                args={
                    "title": f"Kickoff with cofounder — {name}",
                    "start": tomorrow_9am.isoformat(),
                    "duration_minutes": 30,
                    "description": (
                        f"Day-1 plan walkthrough for {name}. Bring "
                        f"questions on the validation report, landing "
                        f"copy, and outreach drafts in your queue."
                    ),
                    "attendees": [founder.email] if founder.email else [],
                },
            )
            session.add(
                Approval(
                    business_id=business.id,
                    agent_role_id=ceo.id,
                    action_class=ActionClass.INTERNAL,
                    proposal_summary=(
                        f"Kickoff invite — tomorrow 09:00 UTC, 30 min"
                    ),
                    action_payload={
                        "kind": "calendar_invite",
                        "niche_name": name,
                        "result": r.payload,
                    },
                    status=ApprovalStatus.PENDING,
                )
            )
            approvals_created += 1
        except (SkillError, Exception) as exc:
            logger.warning("calendar.create_event failed in chain: %s", exc)
            errors.append(f"calendar: {exc}")

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
