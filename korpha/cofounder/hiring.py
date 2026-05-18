"""HiringService: need-based hiring per ARCHITECTURE.md.

| Trigger                         | Hire |
|---------------------------------|------|
| Business created                | CEO  |
| First build/deploy/code task    | CTO  |
| First launch plan / campaign    | CMO  |
| Ops repetition signal           | COO  |
| Specialist need                 | Worker (specialty set) |

Only one active C-suite agent per role per business. Founder can override
explicitly via `hire`/`fire`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import Activity, ActorType
from korpha.cofounder.model import AgentRole, RoleType
from korpha.db._base import utcnow


class HiringTrigger(StrEnum):
    BUILD_TASK_CREATED = "build_task_created"
    LAUNCH_PLAN_CREATED = "launch_plan_created"
    OPS_REPETITION = "ops_repetition"


_TRIGGER_TO_ROLE: dict[HiringTrigger, RoleType] = {
    HiringTrigger.BUILD_TASK_CREATED: RoleType.CTO,
    HiringTrigger.LAUNCH_PLAN_CREATED: RoleType.CMO,
    HiringTrigger.OPS_REPETITION: RoleType.COO,
}

_DEFAULT_TITLES: dict[RoleType, str] = {
    RoleType.CEO: "CEO",
    RoleType.CTO: "CTO",
    RoleType.CMO: "CMO",
    RoleType.COO: "COO",
    RoleType.CHIEF_OF_STAFF: "Chief of Staff",
    RoleType.WORKER: "Worker",
}


@dataclass
class HiringService:
    session: Session

    def ensure_ceo(self, business_id: UUID, *, title: str = "CEO") -> AgentRole:
        existing = self.get_active_role(business_id, RoleType.CEO)
        if existing is not None:
            self._ensure_chief_of_staff(business_id)
            return existing
        ceo = self.hire(
            business_id, RoleType.CEO, title=title, source="business_created"
        )
        # CoS is the internal partner of CEO — always present, never user-facing.
        self._ensure_chief_of_staff(business_id)
        return ceo

    def _ensure_chief_of_staff(self, business_id: UUID) -> AgentRole:
        existing = self.get_active_role(business_id, RoleType.CHIEF_OF_STAFF)
        if existing is not None:
            return existing
        return self.hire(
            business_id,
            RoleType.CHIEF_OF_STAFF,
            title="Chief of Staff",
            source="auto_internal",
        )

    def hire(
        self,
        business_id: UUID,
        role_type: RoleType,
        *,
        title: str | None = None,
        specialty: str | None = None,
        description: str | None = None,
        source: str = "manual",
        reason: str | None = None,
        founder_id: UUID | None = None,
        business_unit_id: UUID | None = None,
    ) -> AgentRole:
        if role_type != RoleType.WORKER:
            existing = self.get_active_role(business_id, role_type)
            if existing is not None:
                # PR-INT-1: if caller specified a unit and the existing
                # role has no unit, scope it. Don't overwrite a different
                # existing unit assignment.
                if (
                    business_unit_id is not None
                    and existing.business_unit_id is None
                ):
                    existing.business_unit_id = business_unit_id
                    self.session.add(existing)
                    self.session.commit()
                    self.session.refresh(existing)
                return existing

        role = AgentRole(
            business_id=business_id,
            business_unit_id=business_unit_id,
            role_type=role_type,
            title=title or _DEFAULT_TITLES.get(role_type, role_type.value.upper()),
            specialty=specialty,
            description=description,
            is_active=True,
        )
        self.session.add(role)
        self.session.commit()
        self.session.refresh(role)

        self._log(
            business_id=business_id,
            actor_id=role.id,
            event_type="agent.hired",
            payload={
                "role_type": role_type.value,
                "title": role.title,
                "specialty": specialty,
                "source": source,
                "reason": reason,
            },
        )
        # Fire the hook so plugins / channels can ping the founder.
        # Best-effort: fire-and-forget; never blocks the hire path.
        self._dispatch_hire_hook(
            role=role, source=source, reason=reason,
            founder_id=founder_id,
        )
        return role

    def _dispatch_hire_hook(
        self,
        *,
        role: AgentRole,
        source: str,
        reason: str | None,
        founder_id: UUID | None,
    ) -> None:
        try:
            from korpha.plugins.hooks import (
                HookKind, WorkerHiredEvent, hook_registry,
            )
        except Exception:  # noqa: BLE001
            return
        if not hook_registry.has(HookKind.WORKER_HIRED):
            return

        event = WorkerHiredEvent(
            business_id=role.business_id,
            founder_id=founder_id,
            agent_role_id=role.id,
            title=role.title,
            specialty=role.specialty,
            role_type=role.role_type.value,
            source=source,
            reason=reason,
        )
        # The hook system expects async dispatch. Run synchronously
        # via asyncio if there's no loop, or schedule on an existing
        # loop without blocking.
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(
                    hook_registry.dispatch(
                        HookKind.WORKER_HIRED, event,
                    )
                )
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "worker_hired hook dispatch failed",
                    exc_info=True,
                )
        else:
            # Inside a loop — schedule it without awaiting.
            loop.create_task(
                hook_registry.dispatch(
                    HookKind.WORKER_HIRED, event,
                )
            )

    def fire(self, agent_role_id: UUID, *, reason: str | None = None) -> AgentRole:
        role = self.session.get(AgentRole, agent_role_id)
        if role is None:
            raise KeyError(f"AgentRole {agent_role_id} not found")
        if not role.is_active:
            return role
        role.is_active = False
        role.fired_at = utcnow()
        self.session.add(role)
        self.session.commit()
        self.session.refresh(role)
        self._log(
            business_id=role.business_id,
            actor_id=role.id,
            event_type="agent.fired",
            payload={"role_type": role.role_type.value, "reason": reason},
        )
        return role

    def get_active_role(
        self, business_id: UUID, role_type: RoleType
    ) -> AgentRole | None:
        stmt = select(AgentRole).where(
            AgentRole.business_id == business_id,
            AgentRole.role_type == role_type,
            AgentRole.is_active == True,  # noqa: E712  (SQL boolean comparison)
        )
        return self.session.exec(stmt).first()

    def trigger_hire_if_needed(
        self, business_id: UUID, trigger: HiringTrigger
    ) -> AgentRole | None:
        """Auto-hire the role implied by a trigger, if not already active."""
        role_type = _TRIGGER_TO_ROLE[trigger]
        existing = self.get_active_role(business_id, role_type)
        if existing is not None:
            return None
        return self.hire(business_id, role_type, source=trigger.value)

    def _log(
        self,
        *,
        business_id: UUID,
        actor_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        activity = Activity(
            business_id=business_id,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            event_type=event_type,
            payload=payload,
        )
        self.session.add(activity)
        self.session.commit()
