"""``hr.start_business_line`` + ``hr.spawn_*`` + ``hr.pause/resume/archive_unit``.

Agent-callable skills that grow the recursive BusinessUnit tree
(PR1) and (eventually) load Line Pack playbooks (PR11). Each skill
wraps ``BusinessUnitBoard`` operations + emits an Activity event +
returns the new unit's ID for chaining.

``niche.score_fit`` lands in this module too — same surface namespace
(both are unit-life-cycle operations).

Spawning a Line VP / Type Mgr / Audience Mgr / Product VP creates the
BusinessUnit but does NOT yet hire the owner agent — that's a follow-
up PR after CEO routing decides which agent kind owns which unit
type. v1 leaves ``owner_agent_role_id`` null on spawn; future PR6
follow-up wires HiringService.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from korpha.audit.model import InferenceTier
from korpha.business_units.board import (
    BusinessUnitBoard, BusinessUnitError,
)
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, NicheProfile,
)
from korpha.business_units.scoring import score_fit
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance,
    SkillResult, SkillSpec,
)


_LINE_KINDS = {
    "pod": "POD",
    "kdp": "KDP",
    "info": "Info Products",
    "saas": "SaaS",
    "affiliate": "Affiliate",
    "agency": "Agency",
}

# Map BusinessUnit kind → AgentRole title prefix for the auto-hired
# owner agent. PR-INT-1 hires WORKER-typed agents as owners so the
# canonical C-suite slots (CEO/CTO/CMO/COO) remain reserved.
_OWNER_TITLE_BY_KIND = {
    BusinessUnitKind.LINE: "Line VP",
    BusinessUnitKind.TYPE: "Type Manager",
    BusinessUnitKind.SERIES: "Series Lead",
    BusinessUnitKind.NICHE: "Niche Manager",
    BusinessUnitKind.AUDIENCE: "Audience Manager",
    BusinessUnitKind.PRODUCT_VP: "Product VP",
}


# ---------------------------------------------------------------------------
# hr.start_business_line
# ---------------------------------------------------------------------------


class StartBusinessLineSkill(Skill):
    """Start a new Line (POD / KDP / Info / SaaS / Affiliate / Agency).

    Creates the BusinessUnit with kind=LINE under the business's
    default unit (or specified parent). Caller can supply a playbook
    skill-pack ID for future install (Line Packs land in PR11).
    """

    spec = SkillSpec(
        name="hr.start_business_line",
        description=(
            "Start a new business line under this Business. Lines "
            "are: pod | kdp | info | saas | affiliate | agency. "
            "Creates the LINE BusinessUnit; future PRs hire its Line "
            "VP and install the playbook. Use when the founder "
            "decides to add a new business model to the portfolio."
        ),
        parameters={
            "kind": (
                "pod | kdp | info | saas | affiliate | agency"
            ),
            "name": (
                "Display name. Optional — defaults to the kind's "
                "canonical label."
            ),
            "parent_unit_id": (
                "Optional UUID of the parent unit. Defaults to the "
                "business's DEFAULT root unit."
            ),
            "playbook_skill_pack": (
                "Optional skill-pack ID from the hub "
                "(e.g. 'kdp-line-pack@1.0.0')."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        kind_str = str(args.get("kind") or "").strip().lower()
        if kind_str not in _LINE_KINDS:
            raise SkillError(
                f"hr.start_business_line: kind must be one of "
                f"{sorted(_LINE_KINDS)}, got {kind_str!r}"
            )
        board = BusinessUnitBoard(ctx.session)
        # Find parent — explicit or default
        parent_id_arg = args.get("parent_unit_id")
        if parent_id_arg:
            parent_id = UUID(str(parent_id_arg))
        else:
            parent = _find_default_unit(board, ctx.business.id)
            if parent is None:
                raise SkillError(
                    "No default unit for this business; run migration "
                    "or create one explicitly via parent_unit_id."
                )
            parent_id = parent.id

        name = str(args.get("name") or _LINE_KINDS[kind_str])
        playbook = args.get("playbook_skill_pack")

        try:
            unit = board.create(
                business_id=ctx.business.id,
                name=name,
                kind=BusinessUnitKind.LINE,
                parent_id=parent_id,
                playbook_skill_pack=str(playbook) if playbook else None,
            )
        except BusinessUnitError as exc:
            raise SkillError(str(exc)) from exc

        # PR-INT-25: actually apply the matching Line Pack's defaults
        # (niche_profile, KPI definitions, suggested workers,
        # required_services). Previously the registry existed but was
        # never consulted at spawn — the unit landed with empty
        # niche_profile.
        try:
            from korpha.line_packs import default_registry as _line_packs
            matching = [
                p for p in _line_packs.all() if p.line_kind == kind_str
            ]
            if matching:
                matching[0].setup_unit(ctx.session, unit)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "Line Pack apply failed for kind=%s unit=%s",
                kind_str, unit.id, exc_info=True,
            )

        # PR-INT-1: hire the Line VP owner agent and wire it onto the
        # unit. WORKER-typed so it doesn't collide with the singleton
        # C-suite slots.
        owner = _hire_owner_agent(
            ctx, unit, title_prefix=_OWNER_TITLE_BY_KIND[unit.kind],
            specialty=f"{kind_str}-line-vp",
        )

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Started {name} ({kind_str}) under business",
            payload={
                "unit_id": str(unit.id),
                "kind": kind_str,
                "name": name,
                "parent_unit_id": str(parent_id),
                "playbook_skill_pack": (
                    str(playbook) if playbook else None
                ),
                "owner_agent_role_id": str(owner.id),
                "owner_title": owner.title,
            },
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# hr.spawn_type_manager
# ---------------------------------------------------------------------------


class SpawnTypeManagerSkill(Skill):
    """Spawn a Type Manager unit under a Line.

    Examples: Romance / Coloring / Cookbook under KDP. T-Shirts /
    Mugs under POD. The owner agent gets the line-specific Type
    playbook (Romance Type Pack, Coloring Type Pack, etc.).
    """

    spec = SkillSpec(
        name="hr.spawn_type_manager",
        description=(
            "Spawn a Type Manager unit under a Line. Use when a Line "
            "has internal categories with distinct playbooks "
            "(KDP Romance vs Coloring, POD T-Shirts vs Mugs)."
        ),
        parameters={
            "parent_unit_id": "UUID of the Line unit",
            "name": "Type name — e.g. 'Romance', 'Coloring', 'T-Shirts'",
            "playbook_skill_pack": "Optional Type Pack ID",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        return await _spawn_child(
            self.spec.name, ctx, args,
            kind=BusinessUnitKind.TYPE,
            required_args=("parent_unit_id", "name"),
        )


# ---------------------------------------------------------------------------
# hr.spawn_audience_manager
# ---------------------------------------------------------------------------


class SpawnAudienceManagerSkill(Skill):
    """Spawn an Audience Manager unit (for Affiliate Line).

    Each list segment in the affiliate line is its own unit because
    each carries its own niche profile + JV calendar.
    """

    spec = SkillSpec(
        name="hr.spawn_audience_manager",
        description=(
            "Spawn an Audience Manager under the Affiliate Line "
            "(or any Line where audience segmentation matters). "
            "Carries its own niche_profile; future compatibility "
            "checks on incoming JV invitations run against it."
        ),
        parameters={
            "parent_unit_id": "UUID of the parent (Affiliate Line)",
            "name": "Audience name — e.g. 'AI marketers'",
            "niche_profile": (
                "Optional dict matching NicheProfile shape "
                "(core_topics, adjacent_topics, off_limits_topics, "
                "persona, list_size, …)."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        profile_data = args.get("niche_profile")
        profile: NicheProfile | None = None
        if isinstance(profile_data, dict):
            try:
                profile = NicheProfile.model_validate(profile_data)
            except Exception as exc:  # noqa: BLE001
                raise SkillError(
                    f"hr.spawn_audience_manager: niche_profile "
                    f"validation failed: {exc}"
                ) from exc

        return await _spawn_child(
            self.spec.name, ctx, args,
            kind=BusinessUnitKind.AUDIENCE,
            required_args=("parent_unit_id", "name"),
            niche_profile=profile,
        )


# ---------------------------------------------------------------------------
# hr.spawn_product_vp
# ---------------------------------------------------------------------------


class SpawnProductVpSkill(Skill):
    """Spawn a Product VP unit (for SaaS Line, primarily).

    SaaS Line has one Product VP per app — owns roadmap, marketing,
    pricing, support for that single product.
    """

    spec = SkillSpec(
        name="hr.spawn_product_vp",
        description=(
            "Spawn a Product VP unit. Mainly for SaaS apps — one "
            "VP per app owning its full lifecycle."
        ),
        parameters={
            "parent_unit_id": "UUID of the parent (typically SaaS Line)",
            "name": "Product name — e.g. 'Korpha', 'RankMyAnswer'",
            "playbook_skill_pack": "Optional product-specific pack",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        return await _spawn_child(
            self.spec.name, ctx, args,
            kind=BusinessUnitKind.PRODUCT_VP,
            required_args=("parent_unit_id", "name"),
        )


# ---------------------------------------------------------------------------
# hr.pause/resume/archive_business_unit
# ---------------------------------------------------------------------------


class PauseBusinessUnitSkill(Skill):
    spec = SkillSpec(
        name="hr.pause_business_unit",
        description=(
            "Pause a unit — blocks new card claims on the unit and "
            "its descendants until resumed. Use during line wind-down "
            "or seasonal lulls."
        ),
        parameters={
            "unit_id": "UUID of the unit",
            "reason": "Why pause — shown in monthly review",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        unit_id = UUID(str(args["unit_id"]))
        reason = args.get("reason")
        try:
            unit = BusinessUnitBoard(ctx.session).pause(
                unit_id, reason=reason,
            )
        except BusinessUnitError as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Paused {unit.name}: {reason or 'no reason'}",
            payload={"unit_id": str(unit.id), "status": unit.status},
            cost_usd=0.0,
        )


class ResumeBusinessUnitSkill(Skill):
    spec = SkillSpec(
        name="hr.resume_business_unit",
        description="Resume a paused unit. Inverse of pause.",
        parameters={"unit_id": "UUID of the unit"},
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        unit_id = UUID(str(args["unit_id"]))
        try:
            unit = BusinessUnitBoard(ctx.session).resume(unit_id)
        except BusinessUnitError as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Resumed {unit.name}",
            payload={"unit_id": str(unit.id), "status": unit.status},
            cost_usd=0.0,
        )


class ArchiveBusinessUnitSkill(Skill):
    spec = SkillSpec(
        name="hr.archive_business_unit",
        description=(
            "Archive (soft-delete) a unit. Refuses if live children "
            "exist; use cascade=True to archive subtree top-down."
        ),
        parameters={
            "unit_id": "UUID of the unit",
            "cascade": "If true, archive all descendants too",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        unit_id = UUID(str(args["unit_id"]))
        cascade = bool(args.get("cascade", False))
        board = BusinessUnitBoard(ctx.session)
        try:
            if cascade:
                archived = board.archive_subtree(unit_id)
                count = len(archived)
                summary = f"Archived {count} units (subtree)"
            else:
                unit = board.archive(unit_id)
                count = 1
                summary = f"Archived {unit.name}"
        except BusinessUnitError as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={"unit_id": str(unit_id), "archived_count": count},
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# niche.score_fit
# ---------------------------------------------------------------------------


class ScoreNicheFitSkill(Skill):
    """Score a piece of work against a BusinessUnit's niche profile.

    Used by Line VPs / Audience Managers to decide whether incoming
    work (new affiliate campaign, new product idea, new JV invitation)
    fits the unit's audience. Verdict drives accept/decline/escalate
    routing.
    """

    spec = SkillSpec(
        name="niche.score_fit",
        description=(
            "Score a new piece of work against the unit's niche "
            "profile. Returns score 0-1 + verdict (accept | decline "
            "| escalate). Deterministic — no LLM in v1."
        ),
        parameters={
            "unit_id": "UUID of the BusinessUnit being evaluated",
            "work_topics": "List of topic tags the work covers",
            "work_summary": "Optional short summary of the work",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        unit_id = UUID(str(args["unit_id"]))
        topics = args.get("work_topics") or []
        if not isinstance(topics, list):
            raise SkillError(
                "niche.score_fit: work_topics must be a list of strings"
            )

        unit = BusinessUnitBoard(ctx.session).get(unit_id)
        if unit is None:
            raise SkillError(f"unit {unit_id} not found")
        if unit.niche_profile is None:
            return SkillResult(
                skill_name=self.spec.name,
                summary="No niche profile set — defer to founder",
                payload={
                    "score": 0.0, "verdict": "escalate",
                    "reason": "unit has no niche_profile yet",
                },
                cost_usd=0.0,
            )
        profile = NicheProfile.model_validate(unit.niche_profile)
        fit = score_fit(profile, [str(t) for t in topics])
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"{fit.verdict.value}: {fit.reason}",
            payload={
                "score": fit.score,
                "verdict": fit.verdict.value,
                "reason": fit.reason,
                "base_score": fit.base_score,
                "fatigue_penalty": fit.fatigue_penalty,
                "density_penalty": fit.density_penalty,
                "off_limits_hit": fit.off_limits_hit,
            },
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _find_default_unit(board: BusinessUnitBoard, business_id: UUID):
    """Return the business's DEFAULT root unit, or None if not found."""
    for unit in board.list_for_business(business_id, include_archived=False):
        if (
            unit.parent_id is None
            and unit.kind == BusinessUnitKind.DEFAULT
        ):
            return unit
    return None


async def _spawn_child(
    skill_name: str,
    ctx: SkillContext,
    args: dict[str, Any],
    *,
    kind: BusinessUnitKind,
    required_args: tuple[str, ...],
    niche_profile: NicheProfile | None = None,
) -> SkillResult:
    """Shared spawn-child path for type/audience/product_vp manager skills."""
    for req in required_args:
        if not args.get(req):
            raise SkillError(f"{skill_name}: {req} is required")

    parent_id = UUID(str(args["parent_unit_id"]))
    name = str(args["name"]).strip()
    playbook = args.get("playbook_skill_pack")

    board = BusinessUnitBoard(ctx.session)
    parent = board.get(parent_id)
    if parent is None:
        raise SkillError(f"{skill_name}: parent {parent_id} not found")
    if parent.business_id != ctx.business.id:
        raise SkillError(
            f"{skill_name}: parent belongs to a different business"
        )

    try:
        unit = board.create(
            business_id=ctx.business.id,
            name=name,
            kind=kind,
            parent_id=parent_id,
            playbook_skill_pack=str(playbook) if playbook else None,
            niche_profile=niche_profile,
        )
    except BusinessUnitError as exc:
        raise SkillError(str(exc)) from exc

    # PR-INT-1: hire the owner agent for the new unit.
    title_prefix = _OWNER_TITLE_BY_KIND.get(kind, "Owner")
    owner = _hire_owner_agent(
        ctx, unit, title_prefix=title_prefix,
        specialty=f"{kind.value}-owner",
    )

    return SkillResult(
        skill_name=skill_name,
        summary=f"Spawned {kind.value}: {name}",
        payload={
            "unit_id": str(unit.id),
            "kind": kind.value,
            "name": name,
            "parent_unit_id": str(parent_id),
            "owner_agent_role_id": str(owner.id),
            "owner_title": owner.title,
        },
        cost_usd=0.0,
    )


def _hire_owner_agent(
    ctx: SkillContext,
    unit: BusinessUnit,
    *,
    title_prefix: str,
    specialty: str,
) -> AgentRole:
    """Hire the agent that owns this BusinessUnit and wire the
    owner_agent_role_id back onto the unit.

    Owner agents are WORKER-typed so they don't collide with the
    singleton C-suite slots (CEO/CTO/CMO/COO/CFO/CHIEF_OF_STAFF).
    Specialty encodes the unit kind so dispatch + monthly review can
    distinguish a 'kdp-line-vp' from a 'pod-line-vp' from a generic
    worker.
    """
    title = f"{title_prefix}: {unit.name}"
    owner = HiringService(ctx.session).hire(
        business_id=ctx.business.id,
        role_type=RoleType.WORKER,
        title=title,
        specialty=specialty,
        source="auto_unit_spawn",
        reason=f"Auto-hired owner for {unit.kind.value} '{unit.name}'",
        business_unit_id=unit.id,
    )
    # Wire onto the unit
    unit.owner_agent_role_id = owner.id
    ctx.session.add(unit)
    ctx.session.commit()
    ctx.session.refresh(unit)
    return owner


class DelegateToVpSkill(Skill):
    """CEO delegates a Line-scoped task to that Line's VP.

    The VP runs synchronously in its own unit namespace — memory
    writes, recall queries, and ask_about calls during the VP's
    turn all auto-scope to the VP's unit. The VP's reply comes back
    to the CEO to incorporate into the founder-facing response.
    """

    spec = SkillSpec(
        name="hr.delegate_to_vp",
        description=(
            "Hand a specific task to the VP of a BusinessUnit. The "
            "VP runs ONE agent turn in its unit's scope and returns "
            "its result. Use whenever the work is Line-scoped: "
            "'KDP should write a launch checklist', 'POD should "
            "evaluate this design', 'Affiliate should draft an "
            "outreach list'. Pass the unit by name (e.g. 'Romance "
            "KDP') or UUID."
        ),
        parameters={
            "unit": (
                "Name OR UUID of the target BusinessUnit. The VP "
                "of this unit will run the task."
            ),
            "task": (
                "The exact task for the VP. Be specific — include "
                "what to do, what to produce, and any constraints. "
                "The VP is execution-focused, not strategic."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from korpha.business_units.context import resolve_unit_id
        from korpha.cofounder.vp_runner import run_vp_turn

        task = str(args.get("task") or "").strip()
        if not task:
            raise SkillError("hr.delegate_to_vp: task required")
        unit_arg = args.get("unit")
        if not unit_arg:
            raise SkillError("hr.delegate_to_vp: unit required")
        try:
            unit_id = resolve_unit_id(
                ctx.session, ctx.business.id, unit_arg,
            )
        except ValueError as exc:
            raise SkillError(
                f"hr.delegate_to_vp: {exc}"
            ) from exc
        if unit_id is None:
            raise SkillError(
                "hr.delegate_to_vp: could not resolve unit"
            )

        vp_result = await run_vp_turn(
            session=ctx.session,
            business=ctx.business,
            founder=ctx.founder,
            unit_id=unit_id,
            task=task,
            cost_tracker=ctx.cost_tracker,
        )

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Delegated to {vp_result.unit_name}: "
                f"{(vp_result.content or '')[:120]}"
            ),
            payload={
                "unit_id": str(vp_result.unit_id),
                "unit_name": vp_result.unit_name,
                "vp_agent_role_id": (
                    str(vp_result.vp_agent_role_id)
                    if vp_result.vp_agent_role_id else None
                ),
                "vp_reply": vp_result.content,
                "skills_used_by_vp": [
                    s.skill_name for s in vp_result.skills_used
                ],
            },
            cost_usd=vp_result.cost_usd,
        )


register(StartBusinessLineSkill())
register(SpawnTypeManagerSkill())
register(SpawnAudienceManagerSkill())
register(SpawnProductVpSkill())
register(PauseBusinessUnitSkill())
register(ResumeBusinessUnitSkill())
register(ArchiveBusinessUnitSkill())
register(ScoreNicheFitSkill())
register(DelegateToVpSkill())


__all__ = [
    "ArchiveBusinessUnitSkill",
    "DelegateToVpSkill",
    "PauseBusinessUnitSkill",
    "ResumeBusinessUnitSkill",
    "ScoreNicheFitSkill",
    "SpawnAudienceManagerSkill",
    "SpawnProductVpSkill",
    "SpawnTypeManagerSkill",
    "StartBusinessLineSkill",
]
