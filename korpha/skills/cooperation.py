"""``cooperation.*`` skills — propose / decide / escalate / ask_about.

The cooperation API for cross-unit work. Built on CooperationBoard
(PR8) — proposes voluntary inter-unit terms, decides accept/decline,
escalates to CEO or founder on disagreement. ``ask_about`` is the
phone-call API that lets a unit ask another unit's owner agent a
structured question without granting memory access.

PR8 ships propose / decide / escalate. ``ask_about``'s dispatch-to-
target-agent path is wired via the existing CEO router (#128) and
skill resolver — for v1 it returns a structured stub response that
the target's owner agent fills in at next turn. Production-grade
synchronous dispatch lands when the multi-agent message bus is in
place.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from korpha.audit.model import InferenceTier
from korpha.cooperation.board import (
    CooperationBoard, CooperationError,
)
from korpha.cooperation.model import CooperationStatus
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance,
    SkillResult, SkillSpec,
)


class ProposeCooperationSkill(Skill):
    spec = SkillSpec(
        name="cooperation.propose",
        description=(
            "Propose a cross-unit cooperation. Use when your unit "
            "spots synergy with another (POD merch around a KDP "
            "series; affiliate promo for SaaS launch; etc.). "
            "Voluntary — the target can refuse."
        ),
        parameters={
            "from_unit_id": (
                "Unit name OR UUID of the asking unit. Optional — "
                "defaults to caller's active business_unit_id."
            ),
            "to_unit_id": (
                "Unit name OR UUID of the target unit. Required."
            ),
            "summary": "One-liner summary of the proposal",
            "details": "Optional markdown details",
            "proposed_terms": (
                "Optional dict: royalty split, exclusivity, "
                "payment schedule, etc."
            ),
            "permissions": (
                "Optional dict: cross_tree_query (bool), "
                "cross_namespace_recall (bool), royalty_share_pct, "
                "promo_slot_count."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        from korpha.business_units.context import resolve_unit_id
        try:
            from_unit_id = resolve_unit_id(
                ctx.session, ctx.business.id,
                args.get("from_unit_id"),
            )
            if from_unit_id is None:
                from_unit_id = ctx.business_unit_id
            if from_unit_id is None:
                raise SkillError(
                    "cooperation.propose: from_unit_id required "
                    "(no caller unit context to default from)."
                )
            to_unit_id = resolve_unit_id(
                ctx.session, ctx.business.id,
                args.get("to_unit_id"),
            )
            if to_unit_id is None:
                raise SkillError("cooperation.propose: to_unit_id required")
        except ValueError as exc:
            raise SkillError(f"cooperation.propose: {exc}") from exc
        try:
            prop = CooperationBoard(ctx.session).propose(
                business_id=ctx.business.id,
                from_unit_id=from_unit_id,
                to_unit_id=to_unit_id,
                summary=str(args.get("summary") or "").strip(),
                details=str(args.get("details") or ""),
                proposed_terms=args.get("proposed_terms") or {},
                permissions=args.get("permissions") or {},
            )
        except CooperationError as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Proposed: {prop.summary}",
            payload={
                "proposal_id": str(prop.id),
                "status": prop.status.value,
            },
            cost_usd=0.0,
        )


class DecideCooperationSkill(Skill):
    spec = SkillSpec(
        name="cooperation.decide",
        description=(
            "Accept or decline a cooperation proposal. Target unit's "
            "owner agent (or its delegated decider) calls this."
        ),
        parameters={
            "proposal_id": "UUID of the proposal",
            "decision": (
                "accepted | declined | escalated_ceo"
            ),
            "note": "Optional decision note",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        decision = str(args.get("decision") or "").lower()
        if decision not in {"accepted", "declined", "escalated_ceo"}:
            raise SkillError(
                f"cooperation.decide: decision must be "
                f"accepted | declined | escalated_ceo, got {decision!r}"
            )
        status_map = {
            "accepted": CooperationStatus.ACCEPTED,
            "declined": CooperationStatus.DECLINED,
            "escalated_ceo": CooperationStatus.ESCALATED_CEO,
        }
        try:
            prop = CooperationBoard(ctx.session).decide(
                UUID(str(args["proposal_id"])),
                decision=status_map[decision],
                note=args.get("note"),
                decided_by_agent_role_id=(
                    ctx.invoking_agent_role_id
                    if hasattr(ctx, "invoking_agent_role_id")
                    else None
                ),
            )
        except CooperationError as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Decision: {prop.status.value}",
            payload={
                "proposal_id": str(prop.id),
                "status": prop.status.value,
            },
            cost_usd=0.0,
        )


class EscalateCooperationSkill(Skill):
    spec = SkillSpec(
        name="cooperation.escalate",
        description=(
            "Escalate a borderline cooperation to CEO for arbitration. "
            "If CEO can't decide, it bubbles up to founder via an "
            "Approval with action_class=STRATEGIC."
        ),
        parameters={
            "proposal_id": "UUID of the proposal",
            "note": "Why escalating — context for the CEO",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        try:
            prop = CooperationBoard(ctx.session).decide(
                UUID(str(args["proposal_id"])),
                decision=CooperationStatus.ESCALATED_CEO,
                note=args.get("note"),
            )
        except CooperationError as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Escalated to CEO: {prop.summary}",
            payload={
                "proposal_id": str(prop.id),
                "status": prop.status.value,
            },
            cost_usd=0.0,
        )


class AskAboutSkill(Skill):
    """Phone-call API. Asks another unit's owner agent a structured
    question without granting memory access.

    PR-INT-6: synchronous dispatch — the target unit's owner agent
    is invoked with the question, runs in its OWN memory namespace,
    and returns a structured response. The response is captured back
    on the CrossUnitQueryLog row.
    """

    spec = SkillSpec(
        name="cooperation.ask_about",
        description=(
            "Ask another BusinessUnit's owner agent a structured "
            "question. NEVER grants memory access — the target unit's "
            "agent processes the question with its OWN scoped memory "
            "and returns a response. Authorized for sibling units + "
            "ancestor/descendant; cross-tree queries require an "
            "accepted CooperationProposal granting cross_tree_query."
        ),
        parameters={
            "from_unit_id": (
                "Unit name OR UUID of the asking unit. Optional — "
                "defaults to the caller's active business_unit_id "
                "from context."
            ),
            "to_unit_id": (
                "Unit name OR UUID of the target unit. Required."
            ),
            "question": "Structured question for the target agent",
            "context": "Optional explicit context (not memory)",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        from korpha.business_units.context import resolve_unit_id

        question = str(args.get("question") or "").strip()
        extra_context = args.get("context") or ""
        if not question:
            raise SkillError("cooperation.ask_about: question required")

        # PR-INT-9: resolve names → UUIDs + auto-default from_unit_id
        # to the caller's active unit context.
        try:
            from_unit_id = resolve_unit_id(
                ctx.session, ctx.business.id,
                args.get("from_unit_id"),
            )
            if from_unit_id is None:
                from_unit_id = ctx.business_unit_id
            if from_unit_id is None:
                raise SkillError(
                    "cooperation.ask_about: from_unit_id not provided "
                    "and caller has no unit context. Pass the asking "
                    "unit's name or UUID explicitly."
                )

            to_unit_id = resolve_unit_id(
                ctx.session, ctx.business.id,
                args.get("to_unit_id"),
            )
            if to_unit_id is None:
                raise SkillError(
                    "cooperation.ask_about: to_unit_id required"
                )
        except ValueError as exc:
            raise SkillError(
                f"cooperation.ask_about: {exc}"
            ) from exc

        board = CooperationBoard(ctx.session)
        if not board.ask_about_authorized(
            from_unit_id=from_unit_id, to_unit_id=to_unit_id,
        ):
            raise SkillError(
                f"cross-tree query from {from_unit_id} to "
                f"{to_unit_id} requires an accepted CooperationProposal "
                f"granting cross_tree_query"
            )

        log_row = board.log_query(
            business_id=ctx.business.id,
            from_unit_id=from_unit_id,
            to_unit_id=to_unit_id,
            question_summary=question,
            asked_by_agent_role_id=getattr(
                ctx, "invoking_agent_role_id", None,
            ),
        )

        # PR-INT-6: synchronous dispatch.
        # Run the target unit's owner agent against the question with
        # ITS namespace as the SkillContext.business_unit_id. The
        # dispatcher is a simple in-process function — the target
        # agent role isn't actually "executed" yet (full agent runtime
        # invocation needs the inference pool which requires
        # credentials wired). v1 dispatch returns a structured
        # acknowledgment that includes the target's namespace + any
        # memory matches the target found for the question.
        from korpha.cooperation.dispatch import dispatch_ask_about
        response = await dispatch_ask_about(
            ctx=ctx,
            from_unit_id=from_unit_id,
            to_unit_id=to_unit_id,
            question=question,
            extra_context=str(extra_context),
        )

        # Update the query log with the response summary
        log_row.response_summary = (response.get("answer") or "")[:200]
        ctx.session.add(log_row)
        ctx.session.commit()

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Asked unit {to_unit_id}: "
                f"{response.get('answer', '')[:80]}"
            ),
            payload={
                "query_log_id": str(log_row.id),
                "status": "answered",
                "from_unit_id": str(from_unit_id),
                "to_unit_id": str(to_unit_id),
                "response": response,
            },
            cost_usd=0.0,
        )


register(ProposeCooperationSkill())
register(DecideCooperationSkill())
register(EscalateCooperationSkill())
register(AskAboutSkill())


__all__ = [
    "AskAboutSkill",
    "DecideCooperationSkill",
    "EscalateCooperationSkill",
    "ProposeCooperationSkill",
]
