"""SkillRegistry — central index of available skills.

There's a process-wide ``default_registry`` that built-in skills self-
register into via ``@register``. Tests construct their own registry to
stay isolated; embedding hosts can do the same to scope which skills a
given Founder/business can see.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillNotFound,
    SkillResult,
    SkillSpec,
)


@dataclass
class SkillRegistry:
    skills: dict[str, Skill] = field(default_factory=dict)

    def add(self, skill: Skill) -> None:
        if skill.spec.name in self.skills:
            raise ValueError(
                f"Skill {skill.spec.name!r} already registered "
                "(remove the duplicate registration)"
            )
        self.skills[skill.spec.name] = skill

    def get(self, name: str) -> Skill:
        skill = self.skills.get(name)
        if skill is None:
            raise SkillNotFound(f"Skill {name!r} not found")
        return skill

    def list_specs(
        self, *, include_unsupported: bool = False,
    ) -> list[SkillSpec]:
        """Return the specs the CEO router sees.

        Filters out skills whose ``platforms`` whitelist excludes the
        current OS — Mac-only AppleScript skills never appear in the
        Linux VPS's catalog, so the LLM never picks one and crashes
        on missing ``osascript``. Pass ``include_unsupported=True``
        for diagnostic listings (``korpha doctor``, dashboard
        admin) that want the full set."""
        out: list[SkillSpec] = []
        for skill in self.skills.values():
            if include_unsupported or skill.spec.supports_current_platform():
                out.append(skill.spec)
        return out

    async def run(
        self,
        name: str,
        *,
        ctx: SkillContext,
        args: dict[str, Any],
        invoking_agent_role_id: UUID | None = None,
    ) -> SkillResult:
        if invoking_agent_role_id is not None:
            ctx.invoking_agent_role_id = invoking_agent_role_id
        skill = self.get(name)
        # Defense in depth: even if a stale router prompt picks an
        # OS-restricted skill, refuse before the skill hits a missing
        # binary and crashes the turn.
        if not skill.spec.supports_current_platform():
            import sys

            from korpha.skills.types import SkillError
            raise SkillError(
                f"Skill {name!r} requires platform "
                f"{list(skill.spec.platforms)!r} but this host is "
                f"{sys.platform!r}."
            )

        # Plugin lifecycle hooks (pre + post). Lazy import to avoid
        # a cold-load cost on installs without any plugins. Only
        # dispatches when a hook is actually registered for the
        # kind — keeps the no-plugin path zero-cost.
        import time

        from korpha.plugins.hooks import (
            HookKind, PostSkillCallEvent, PreSkillCallEvent,
            hook_registry,
        )

        business_id = getattr(ctx.business, "id", None)
        founder_id = getattr(ctx.founder, "id", None)
        agent_role_id = getattr(ctx, "invoking_agent_role_id", None)
        if hook_registry.has(HookKind.PRE_SKILL_CALL):
            await hook_registry.dispatch(
                HookKind.PRE_SKILL_CALL,
                PreSkillCallEvent(
                    skill_name=name,
                    args=dict(args),
                    business_id=business_id,
                    founder_id=founder_id,
                    invoking_agent_role_id=agent_role_id,
                ),
            )

        started = time.monotonic()
        result: SkillResult | None = None
        error: BaseException | None = None
        try:
            result = await skill.run(ctx=ctx, args=args)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            if hook_registry.has(HookKind.POST_SKILL_CALL):
                await hook_registry.dispatch(
                    HookKind.POST_SKILL_CALL,
                    PostSkillCallEvent(
                        skill_name=name,
                        args=dict(args),
                        duration_seconds=time.monotonic() - started,
                        business_id=business_id,
                        founder_id=founder_id,
                        invoking_agent_role_id=agent_role_id,
                        result=result,
                        error=error,
                    ),
                )


default_registry = SkillRegistry()


def register(skill: Skill) -> Skill:
    """Decorator-friendly helper. Adds the skill to the default registry."""
    default_registry.add(skill)
    return skill
