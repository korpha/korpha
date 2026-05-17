"""Skill base class + supporting types."""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Session

from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker


class SkillProvenance(StrEnum):
    """Who wrote this skill. Drives the future curator (so it knows
    which skills it's allowed to auto-archive) plus the audit trail
    + dashboard ('agent-authored skills' vs 'built-in')."""

    BUILTIN = "builtin"
    """Shipped with the Korpha distribution. Never auto-archived,
    never approved on creation (it shipped pre-approved)."""

    USER_AUTHORED = "user_authored"
    """Hand-written by the founder via the CLI / file edits.
    Curator leaves these alone."""

    AGENT_AUTHORED = "agent_authored"
    """Drafted by ``meta.author_skill`` / ``meta.author_python_skill``,
    approved through the gate, written into
    ``~/.korpha/skills/agent_created/``. Curator may auto-archive
    after a usage-based decay window."""

    HERMES_PORT = "hermes_port"
    """Faithful port of a skill that ships with Hermes Agent (Nous
    Research). Provenance marker so we can credit upstream + track
    what to re-sync when Hermes ships changes. Never auto-archived."""


@dataclass(frozen=True)
class SkillSpec:
    """Static metadata about a skill — what it's called, what it does, what
    parameters it expects. Used to build CEO prompts and CLI help."""

    name: str
    """Dotted name, e.g. ``niche.find_micro_niches``."""

    description: str
    """Short one-liner. Shown to the LLM when it's choosing skills."""

    parameters: dict[str, str] = field(default_factory=dict)
    """name -> description. What the caller should pass."""

    default_tier: InferenceTier = InferenceTier.PRO

    platforms: tuple[str, ...] = field(default_factory=tuple)
    """OS platforms the skill supports. Empty = no restriction (the
    common case — most skills are LLM-only and platform-independent).
    Names match ``sys.platform`` values: ``"linux"``, ``"darwin"``,
    ``"win32"``. Listing is whitelist semantics: if non-empty, only
    matching platforms expose the skill. AppleScript flows pin to
    ``("darwin",)`` so they don't crash on the Contabo Linux VPS."""

    provenance: SkillProvenance = SkillProvenance.BUILTIN
    """Who authored this skill. Defaults to BUILTIN — codepaths that
    register agent-authored skills set AGENT_AUTHORED explicitly so
    the curator + dashboard can tell them apart."""

    def supports_current_platform(self) -> bool:
        """True if this skill should be exposed on this OS. Empty
        ``platforms`` tuple means 'no restriction'."""
        if not self.platforms:
            return True
        return sys.platform in self.platforms


@dataclass
class SkillContext:
    """Runtime context handed to a skill: who's calling, where, with what."""

    business: Business
    founder: Founder
    session: Session
    cost_tracker: CostTracker
    invoking_agent_role_id: UUID | None = None
    browser: Any | None = None
    """Optional ``BrowserService`` — present when the host has wired a
    browser provider. Skills that need the web check ``ctx.browser`` and
    raise SkillError if it's None.

    Typed as Any to avoid a hard import-cycle with the browser package
    (browser depends on nothing in skills; skills shouldn't depend on
    browser at module-load time)."""

    business_unit_id: UUID | None = None
    """PR-INT-1: the BusinessUnit context for this skill invocation.

    Set by the workforce dispatcher from the assigned kanban card's
    business_unit_id (or the invoking agent's primary unit when no
    card is in play). Skills that need unit scoping — memory.recall
    (namespace), credentials resolver (per-unit creds), shared-resource
    usage (consumer attribution) — read this field. Null falls through
    to company-default behavior."""


@dataclass(frozen=True)
class SkillResult:
    """Structured output from a skill invocation."""

    skill_name: str
    summary: str
    """One-line summary of what the skill produced."""

    payload: dict[str, Any]
    """Skill-specific structured data — the actual result the caller uses."""

    cost_usd: float = 0.0
    reasoning: str | None = None
    raw_response: str = ""


class SkillError(Exception):
    """Thrown by a skill when it cannot proceed (bad args, dependency
    missing, model returned junk, etc.)."""


class SkillNotFound(SkillError):
    """Raised when SkillRegistry.get() can't find the named skill."""


class Skill(ABC):
    """Base class for all skills. Subclasses implement ``run``."""

    spec: SkillSpec

    @abstractmethod
    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        """Execute the skill. Return a SkillResult or raise SkillError."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "spec") or cls.spec is None:  # pragma: no cover
            raise TypeError(f"{cls.__name__} must define a `spec: SkillSpec`")
