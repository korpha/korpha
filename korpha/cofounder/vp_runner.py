"""VP agent runner — one scoped agent turn for a Line VP.

The CEO can call ``hr.delegate_to_vp`` to hand a task to the VP of
a specific BusinessUnit. This module is what actually runs the VP
turn: builds a unit-scoped SkillContext, invokes the same router →
skill → synth pattern as CEO.handle(), but with:

* business_unit_id pinned to the VP's unit so memory.remember /
  memory.recall / cooperation.ask_about auto-scope correctly
* the VP's own agent_role_id for cost attribution
* a system prompt that identifies the agent as Line VP of <unit>
  with the unit's niche profile + line pack context

The VP runner is **synchronous to the CEO call** — when the CEO
delegates, it blocks until the VP returns. This keeps the chat
turn coherent for the founder; long-running VP work goes through
the kanban + workforce paths instead (PR-INT-15).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import Activity, ActorType, InferenceTier
from korpha.business.model import Business
from korpha.business_units.model import BusinessUnit
from korpha.cofounder.model import AgentRole
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.types import CompletionRequest, Message, Role
from korpha.skills import default_registry
from korpha.skills.registry import SkillRegistry
from korpha.skills.types import (
    SkillContext, SkillError, SkillResult,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VpRunResult:
    """What the runner returns to the CEO."""

    content: str
    """The VP's final reply."""

    unit_id: UUID
    unit_name: str
    vp_agent_role_id: UUID | None

    skills_used: list[SkillResult]
    cost_usd: float
    reasoning: str | None = None


def _build_vp_system_prompt(
    business: Business, unit: BusinessUnit, vp: AgentRole | None,
) -> str:
    """System prompt that puts the LLM in the VP's shoes."""
    vp_title = vp.title if vp is not None else f"Line VP: {unit.name}"
    parts = [
        f"You are {vp_title} for {business.name}.",
        (
            f"Your business unit: **{unit.name}** "
            f"(kind={unit.kind.value}, status={unit.status})."
        ),
        (
            "You report to the CEO. The CEO has just delegated a "
            "specific task to you. Execute it within your unit's "
            "scope — every memory write, every cooperation query "
            "you make is automatically scoped to your unit's "
            "namespace (no extra args needed)."
        ),
    ]
    # Niche profile snippet if present
    if unit.niche_profile:
        try:
            np = unit.niche_profile
            core = ", ".join(np.get("core_topics") or [])[:200]
            persona = (np.get("persona") or "")[:200]
            if core or persona:
                parts.append(
                    f"**Your niche:** core topics: {core or '(none)'}. "
                    f"persona: {persona or '(none)'}."
                )
        except Exception:  # noqa: BLE001
            pass
    parts.append(
        "When the task is done, reply with a concise confirmation "
        "(what you did + the result). Do NOT plan or strategize — "
        "the CEO does that. You execute."
    )
    return "\n\n".join(parts)


async def run_vp_turn(
    *,
    session: Session,
    business: Business,
    founder: Founder,
    unit_id: UUID,
    task: str,
    cost_tracker: CostTracker,
    skill_registry: SkillRegistry | None = None,
    tier: InferenceTier = InferenceTier.WORKHORSE,
    max_tokens: int | None = None,
) -> VpRunResult:
    """Run one VP turn end-to-end.

    Imports CEO's router prompt + synth prompt + parser to keep the
    routing semantics identical — only the system prompt + skill
    context differ.
    """
    # Imported here to avoid a top-level cycle (vp_runner ↔ ceo).
    from korpha.cofounder.ceo import (
        _parse_router_decision,
        _skill_router_prompt,
        _skill_synth_prompt,
    )

    registry = skill_registry or default_registry
    unit = session.get(BusinessUnit, unit_id)
    if unit is None:
        raise SkillError(f"vp_runner: BusinessUnit {unit_id} not found")
    if unit.business_id != business.id:
        raise SkillError(
            f"vp_runner: BusinessUnit {unit_id} doesn't belong to "
            f"this business"
        )

    vp_role: AgentRole | None = None
    if unit.owner_agent_role_id is not None:
        vp_role = session.get(AgentRole, unit.owner_agent_role_id)

    system_prompt = _build_vp_system_prompt(business, unit, vp_role)
    skill_specs = registry.list_specs()

    messages = [
        Message(role=Role.SYSTEM, content=system_prompt),
        Message(
            role=Role.USER,
            content=_skill_router_prompt(skill_specs, task),
        ),
    ]

    session_key = f"vp-{unit_id}"
    router_request = CompletionRequest(
        messages=messages,
        tier=tier,
        session_key=session_key,
        max_tokens=max_tokens,
    )
    router_response = await cost_tracker.complete(
        router_request,
        session=session,
        business_id=business.id,
        agent_role_id=vp_role.id if vp_role else None,
        business_unit_id=unit_id,
    )

    decision = _parse_router_decision(router_response.content)
    skills_used: list[SkillResult] = []
    total_cost = float(router_response.cost_usd)

    final_content: str

    if decision is None or decision.action != "use_skill":
        # VP chose to respond directly (no skill needed for this task)
        final_content = (
            (decision.content if decision is not None else None)
            or router_response.content
        )
    else:
        if (
            decision.skill_name is None
            or registry.skills.get(decision.skill_name) is None
        ):
            final_content = (
                f"VP could not invoke skill "
                f"{decision.skill_name!r}: unknown skill name."
            )
        else:
            # Build unit-scoped SkillContext + run the chosen skill.
            skill_ctx = SkillContext(
                business=business,
                founder=founder,
                session=session,
                cost_tracker=cost_tracker,
                invoking_agent_role_id=vp_role.id if vp_role else None,
                business_unit_id=unit_id,
            )
            try:
                result = await registry.run(
                    decision.skill_name,
                    ctx=skill_ctx,
                    args=decision.skill_args or {},
                )
            except SkillError as exc:
                final_content = (
                    f"VP attempted {decision.skill_name} but it "
                    f"failed: {exc}"
                )
                result = None
            if result is not None:
                skills_used.append(result)
                total_cost += float(result.cost_usd)

                # Synth — VP composes the final reply that incorporates
                # the skill output.
                synth_messages = [
                    Message(role=Role.SYSTEM, content=system_prompt),
                    Message(
                        role=Role.USER,
                        content=_skill_synth_prompt(task, result),
                    ),
                ]
                synth_request = CompletionRequest(
                    messages=synth_messages,
                    tier=tier,
                    session_key=session_key,
                    max_tokens=max_tokens,
                )
                synth_response = await cost_tracker.complete(
                    synth_request,
                    session=session,
                    business_id=business.id,
                    agent_role_id=vp_role.id if vp_role else None,
                    business_unit_id=unit_id,
                )
                total_cost += float(synth_response.cost_usd)
                final_content = synth_response.content

    # Audit log — the VP did a thing on behalf of CEO delegation.
    try:
        session.add(Activity(
            business_id=business.id,
            business_unit_id=unit_id,
            actor_type=ActorType.AGENT,
            actor_id=vp_role.id if vp_role else None,
            event_type="vp.task_handled",
            payload={
                "task_summary": task[:200],
                "skills_used": [s.skill_name for s in skills_used],
                "reply_preview": (final_content or "")[:200],
            },
        ))
        session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("vp_runner: activity log failed", exc_info=True)

    return VpRunResult(
        content=final_content,
        unit_id=unit.id,
        unit_name=unit.name,
        vp_agent_role_id=vp_role.id if vp_role else None,
        skills_used=skills_used,
        cost_usd=total_cost,
    )


@dataclass
class VpExecutor:
    """Adapter so the Workforce dispatcher can treat a VP like a
    Director — same ``.attempt(business, founder, task)`` shape,
    returns an AttemptResult-shaped object.

    PR-INT-15: when a kanban card has business_unit_id set + the
    unit has an owner agent, the workforce builds a VpExecutor
    instead of picking a generic Director. The VP then runs in
    its own namespace and the work attributes correctly.
    """

    unit_id: UUID
    session: Session
    cost_tracker: CostTracker
    fallback_role_type: Any = None
    """RoleType to put on the AttemptResult (for kanban
    bookkeeping). The VP isn't a C-suite role; we map to whatever
    the founder/CEO would have routed to as a default."""

    @property
    def personality(self) -> Any:
        """Quack like a Director: workforce reads
        executor.personality.role_type + .title for kanban
        bookkeeping. Returns a tiny SimpleNamespace."""
        from types import SimpleNamespace

        from korpha.cofounder.model import RoleType
        # Pull unit name + role for display
        unit = self.session.get(BusinessUnit, self.unit_id)
        title = (
            f"Line VP: {unit.name}" if unit is not None
            else f"VP[{self.unit_id}]"
        )
        return SimpleNamespace(
            role_type=self.fallback_role_type or RoleType.WORKER,
            title=title,
        )

    async def attempt(
        self,
        *,
        business: Business,
        founder: Founder,
        task: str,
    ):
        """Run one VP turn; return an AttemptResult-shaped object."""
        from korpha.cofounder.director import AttemptResult
        from korpha.cofounder.model import RoleType

        result = await run_vp_turn(
            session=self.session,
            business=business,
            founder=founder,
            unit_id=self.unit_id,
            task=task,
            cost_tracker=self.cost_tracker,
        )
        return AttemptResult(
            role_type=self.fallback_role_type or RoleType.WORKER,
            title=f"Line VP: {result.unit_name}",
            status="shipped",
            summary=(result.content or "")[:200],
            detail=result.content,
            blocker_ids=[],
            raw_response=result.content,
            reasoning=result.reasoning,
            cost_usd=float(result.cost_usd),
        )


__all__ = ["VpExecutor", "VpRunResult", "run_vp_turn"]
