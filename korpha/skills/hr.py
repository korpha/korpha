"""``hr.*`` — agent-callable team management.

The BRIEF "Run" pillar:

    Hires more specialized sub-agents as the business grows.

The HiringService + AgentRole model already support workers with
arbitrary ``specialty`` strings. What was missing was the surface
the cofounder calls when it spots a recurring need ("we keep
needing copy edits — let's hire a copywriter").

  * ``hr.hire_worker(specialty=..., title=..., reason=...)``
    spins up a new WORKER AgentRole. Founder approval is staged
    before the role becomes active in production-ready setups
    (``require_approval=True``); CEO-driven hires bypass when the
    autonomy envelope allows.

  * ``hr.fire_worker(agent_role_id=..., reason=...)``
    deactivates a role when its specialty isn't earning its keep.

  * ``hr.list_team()`` returns the org chart — every active
    role + specialty + when hired. CEO uses this to know who's
    on the bench before tagging tasks.

Workers don't auto-dispatch like the C-suite (that's a deeper
refactor). Their immediate value is org-chart visibility:
* /app/agents lists them
* CEO can mention them by name in plans
* finance.monthly_review counts them in headcount metrics
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from korpha.audit.model import InferenceTier
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)


_VALID_C_SUITE = {
    "cto": RoleType.CTO,
    "cmo": RoleType.CMO,
    "coo": RoleType.COO,
}


class HireWorkerSkill(Skill):
    """Hire a specialized worker agent (e.g. copywriter, ads
    manager, support rep)."""

    spec = SkillSpec(
        name="hr.hire_worker",
        description=(
            "Hire a specialized sub-agent. Use when you spot a "
            "recurring task that the C-suite can't handle "
            "efficiently — e.g. 'we keep writing 5 LinkedIn "
            "drafts a week, let's hire a copywriter'. Specialty "
            "is free-form; pick something concrete the worker "
            "can be tagged with later (copywriter, ads-manager, "
            "support-rep, founder-interviewer). Workers show up "
            "on /app/agents and finance.monthly_review counts."
        ),
        parameters={
            "specialty": (
                "Short specialty tag — 'copywriter' / "
                "'ads-manager' / 'support-rep'. Lowercase, "
                "hyphen-separated, no spaces."
            ),
            "title": (
                "Optional friendly title for the worker. "
                "Defaults to the specialty title-cased."
            ),
            "reason": (
                "Optional. Why this hire is justified now. "
                "Recorded in the audit log for the founder's "
                "monthly review."
            ),
            "description": (
                "Optional 1-3 sentence persona / voice / what "
                "they're good at. Used by the CEO router to pick "
                "between similar-specialty workers (e.g. when you "
                "have two copywriters — one for punchy tweets, "
                "one for long teardowns). Specialty alone is a "
                "keyword; description gives the router context to "
                "route on voice/format/domain. ≤1000 chars."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        specialty = str(args.get("specialty") or "").strip().lower()
        if not specialty:
            raise SkillError("hr.hire_worker: specialty required")
        if " " in specialty:
            raise SkillError(
                "hr.hire_worker: specialty must be one token "
                "(use hyphens), got "
                f"{specialty!r}"
            )
        if len(specialty) > 60:
            raise SkillError(
                "hr.hire_worker: specialty too long (>60 chars)"
            )

        title = str(args.get("title") or "").strip() or (
            specialty.replace("-", " ").title()
        )
        reason = str(args.get("reason") or "").strip() or None
        description = str(args.get("description") or "").strip() or None
        if description and len(description) > 1000:
            description = description[:1000]

        hiring = HiringService(ctx.session)
        role = hiring.hire(
            ctx.business.id,
            RoleType.WORKER,
            title=title,
            specialty=specialty,
            description=description,
            source=(
                f"hr.hire_worker:{reason[:80]}"
                if reason else "hr.hire_worker"
            ),
        )
        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"hired worker '{role.title}' (specialty: {specialty})"
            ),
            payload={
                "agent_role_id": str(role.id),
                "specialty": specialty,
                "title": role.title,
                "reason": reason,
            },
            cost_usd=0.0,
        )


class FireWorkerSkill(Skill):
    """Deactivate a worker that isn't earning its keep."""

    spec = SkillSpec(
        name="hr.fire_worker",
        description=(
            "Deactivate a hired worker. Use when a specialty "
            "isn't earning its keep (no cards claimed, no useful "
            "output) and you want it off the org chart. Only "
            "fires WORKER-typed roles — refuses to fire C-suite "
            "or CEO via this skill (use the explicit `korpha "
            "fire` CLI command for that to make it deliberate)."
        ),
        parameters={
            "agent_role_id": (
                "UUID of the worker AgentRole to fire. Get it "
                "from hr.list_team."
            ),
            "reason": (
                "Optional. Why this worker is being let go. "
                "Recorded in the audit log."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        raw_id = str(args.get("agent_role_id") or "").strip()
        if not raw_id:
            raise SkillError("hr.fire_worker: agent_role_id required")
        try:
            role_id = UUID(raw_id)
        except ValueError as exc:
            raise SkillError(
                f"hr.fire_worker: bad UUID {raw_id!r}"
            ) from exc

        role = ctx.session.get(AgentRole, role_id)
        if role is None or role.business_id != ctx.business.id:
            raise SkillError(
                "hr.fire_worker: role not found or belongs to a "
                "different business"
            )
        if role.role_type != RoleType.WORKER:
            raise SkillError(
                f"hr.fire_worker: refuses to fire role_type="
                f"{role.role_type.value}. Only workers can be "
                "fired via this skill."
            )

        reason = str(args.get("reason") or "").strip() or None
        hiring = HiringService(ctx.session)
        fired = hiring.fire(role_id, reason=reason)
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"fired worker '{fired.title}' "
                    f"(specialty: {fired.specialty})",
            payload={
                "agent_role_id": str(fired.id),
                "specialty": fired.specialty,
                "title": fired.title,
                "reason": reason,
            },
            cost_usd=0.0,
        )


class ListTeamSkill(Skill):
    """Return the active org chart."""

    spec = SkillSpec(
        name="hr.list_team",
        description=(
            "Return every active AgentRole on this business — "
            "C-suite + Chief of Staff + workers. Read-only. Use "
            "before suggesting new hires (don't propose hiring a "
            "copywriter if one is already on the bench) or when "
            "the founder asks who's on the team."
        ),
        parameters={
            "include_inactive": (
                "Optional. 'true' to also include fired roles. "
                "Default: false."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from sqlmodel import select as _select

        include_inactive = (
            str(args.get("include_inactive") or "false").lower()
            in ("true", "1", "yes")
        )
        stmt = (
            _select(AgentRole)
            .where(AgentRole.business_id == ctx.business.id)
        )
        if not include_inactive:
            stmt = stmt.where(AgentRole.is_active)
        rows = list(ctx.session.exec(stmt).all())

        team = [
            {
                "agent_role_id": str(r.id),
                "role_type": r.role_type.value,
                "title": r.title,
                "specialty": r.specialty,
                "active": r.is_active,
                "hired_at": r.hired_at.isoformat() if r.hired_at else None,
            }
            for r in rows
        ]
        c_suite = [t for t in team if t["role_type"] in (
            "ceo", "cto", "cmo", "coo", "chief_of_staff",
        )]
        workers = [t for t in team if t["role_type"] == "worker"]
        summary_parts = [
            f"{len(c_suite)} C-suite",
            f"{len(workers)} worker(s)",
        ]
        if include_inactive:
            inactive = sum(1 for t in team if not t["active"])
            summary_parts.append(f"{inactive} inactive")
        return SkillResult(
            skill_name=self.spec.name,
            summary="team: " + ", ".join(summary_parts),
            payload={
                "team": team,
                "c_suite_count": len(c_suite),
                "worker_count": len(workers),
                "total": len(team),
            },
            cost_usd=0.0,
        )


class SpawnExecutivesSkill(Skill):
    """Spawn C-suite executives (CTO / CMO / COO) for the business.

    Idempotent — if a role already exists active, returns the
    existing one. Used when the founder says 'spawn CTO + CMO',
    'hire a CTO', 'I need a CMO', etc. Replaces the CEO's
    hallucinated 'I'll spawn both now' that never actually fired
    the hire — this skill makes 'spawn' do real DB writes.
    """

    spec = SkillSpec(
        name="hr.spawn_executives",
        description=(
            "Spawn one or more C-suite executives (CTO, CMO, COO) "
            "for the business. USE THIS when the founder asks to "
            "'spawn CTO', 'spawn CMO', 'hire a CTO', 'I need a "
            "CMO', 'spawn CTO and CMO', etc. Idempotent: existing "
            "active roles are returned, not duplicated. Args: "
            "roles=[<list of: cto|cmo|coo>]."
        ),
        parameters={
            "roles": (
                "List of c-suite role types to spawn. Each must "
                "be one of: cto | cmo | coo. e.g. ['cto', 'cmo']."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        raw_roles = args.get("roles") or []
        if isinstance(raw_roles, str):
            # Tolerate 'cto,cmo' or 'cto cmo' strings.
            raw_roles = [
                r.strip().lower() for r in raw_roles
                .replace(",", " ").split() if r.strip()
            ]
        if not isinstance(raw_roles, list) or not raw_roles:
            raise SkillError(
                "hr.spawn_executives: roles=<list> required"
            )
        normalized: list[RoleType] = []
        invalid: list[str] = []
        for r in raw_roles:
            key = str(r).strip().lower()
            if key in _VALID_C_SUITE:
                rt = _VALID_C_SUITE[key]
                if rt not in normalized:
                    normalized.append(rt)
            else:
                invalid.append(key)
        if not normalized:
            raise SkillError(
                "hr.spawn_executives: no valid roles in "
                f"{raw_roles!r}. Valid: cto, cmo, coo."
            )

        hiring = HiringService(ctx.session)
        spawned: list[dict[str, Any]] = []
        for rt in normalized:
            existing = hiring.get_active_role(ctx.business.id, rt)
            was_already_there = existing is not None
            role = hiring.hire(
                ctx.business.id, rt,
                source="hr.spawn_executives",
            )
            spawned.append({
                "role_type": rt.value,
                "agent_role_id": str(role.id),
                "title": role.title,
                "already_existed": was_already_there,
            })

        new_hires = [s for s in spawned if not s["already_existed"]]
        existing_hits = [s for s in spawned if s["already_existed"]]
        parts = []
        if new_hires:
            parts.append(
                "spawned " + ", ".join(
                    s["role_type"].upper() for s in new_hires
                )
            )
        if existing_hits:
            parts.append(
                "(already active: " + ", ".join(
                    s["role_type"].upper() for s in existing_hits
                ) + ")"
            )
        summary = "; ".join(parts) if parts else "no changes"
        if invalid:
            summary += f". Ignored invalid: {invalid}"

        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={
                "spawned": spawned,
                "invalid_roles": invalid,
                "new_count": len(new_hires),
            },
            cost_usd=0.0,
        )


register(HireWorkerSkill())
register(FireWorkerSkill())
register(ListTeamSkill())
register(SpawnExecutivesSkill())


__all__ = [
    "FireWorkerSkill",
    "HireWorkerSkill",
    "ListTeamSkill",
    "SpawnExecutivesSkill",
]
