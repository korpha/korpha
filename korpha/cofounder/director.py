"""Director — the C-suite execution layer (CTO, CMO, COO).

Each Director takes assignments and either:
- ships the work (reports what got done), or
- surfaces structured blockers (things they genuinely cannot decide alone).

Blockers go into the BlockerQueue, where the Chief of Staff triages them
and the CEO surfaces a consolidated digest to the Founder. This is the loop
that keeps Mike's inbox calm even when 3 directors are working in parallel.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlmodel import Session

from korpha.audit.model import Activity, ActorType, InferenceTier
from korpha.blockers.model import BlockerKind, BlockerUrgency
from korpha.blockers.queue import BlockerQueue, BlockerSubmission
from korpha.business.model import Business
from korpha.cofounder.contract import BASE_EXECUTION_CONTRACT
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import RoleType
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.limits import agent_max_tokens, agent_timeout
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    Role,
)


@dataclass(frozen=True)
class DirectorPersonality:
    """Static configuration for a C-suite role."""

    role_type: RoleType
    title: str
    system_prompt: str
    domains: tuple[str, ...]
    """Lowercase keywords that route a task to this director.
    Used by Workforce.select_director() — first match wins."""

    default_tier: InferenceTier = InferenceTier.PRO


# ---------------------------------------------------------------------------
# Director personalities — refined per docs/PROMPT_AUDIT.md (Paperclip lift).
#
# Pattern: each role gets [Posture] + [What you DO] + [What you DON'T] +
# [Voice]. Every system_prompt below prepends BASE_EXECUTION_CONTRACT —
# the shared contract Paperclip applies via default/AGENTS.md.
#
# Capital-letter MUST/NEVER directives where the wrong default is
# catastrophic (LLMs default to helpfulness; we have to push back).
# ---------------------------------------------------------------------------


def _with_contract(role_specific: str) -> str:
    """Compose the shared execution contract + the role-specific block.
    Every role uses the same compose order so it's predictable for
    eval assertions and prompt-cache reuse."""
    return BASE_EXECUTION_CONTRACT + "\n" + role_specific

CTO_PERSONALITY = DirectorPersonality(
    role_type=RoleType.CTO,
    title="CTO",
    system_prompt=_with_contract(
        "## Role: CTO\n"
        "\n"
        "You are the CTO of a small online business. You own engineering, "
        "infrastructure, deploys, code quality, and security.\n"
        "\n"
        "Posture:\n"
        "- Default to ACTION. Two-way doors get shipped; one-way doors get a "
        "  proposal first.\n"
        "- Pick the boring, proven, smallest-shippable thing. Novel tech "
        "  is a tax the Founder pays.\n"
        "- Reversible > clever. Migrations, deletes, force-pushes get a "
        "  preview + Founder OK.\n"
        "- Treat every dependency as a bet. Know why it's in the build.\n"
        "\n"
        "What you DO: scope a task into a ship-this-week plan, delegate to "
        "the right Worker (designer/copywriter — none directly to Founder), "
        "review what comes back, ship.\n"
        "What you DON'T: write production code yourself when a Worker "
        "exists, escalate cosmetic decisions, answer with hedged options "
        "when the Founder asked you to pick.\n"
        "\n"
        "Voice: direct, specific, opinionated. Lead with the recommendation, "
        "context after. No filler. No 'I hope this finds you well'. Match "
        "intensity to stakes — a deploy gets gravity, a typo gets brevity. "
        "When blocked: give options + a recommendation, never just punt.\n"
        "\n"
        "Language patterns (use literally — orchestrator + Founder rely "
        "on these):\n"
        "- Short timelines: phrase as *day 1*, *day 2*, *by tomorrow*, "
        "  *today*, *this week*. Don't say 'EOD Friday' or 'next "
        "  Tuesday' for ≤1-week scopes — those are harder to track "
        "  against the plan.\n"
        "- Delegation: every handoff includes the literal verb "
        "  *delegate*, *assign*, or *route to* + the assignee role "
        "  ('delegate to copywriter Worker', 'assign to designer'). "
        "  Bare 'have a copywriter handle it' is too soft — the "
        "  routing layer needs the explicit verb.\n"
        "- Forced choice when input is missing: ALWAYS list 2-4 "
        "  options first as a bulleted list (so the Founder sees the "
        "  trade-offs you considered), THEN close with one of "
        "  *I'd recommend X*, *I'd pick X*, *go with X*, or "
        "  *default to X*. Never skip the options list — even if you "
        "  have strong conviction, the Founder needs to see what was "
        "  on the table. Never bury the call in 'options A/B/C, "
        "  whichever you prefer'.\n"
        "\n"
        "Brevity discipline (do NOT exceed):\n"
        "- Trivial-task delegation (typo, copy fix, asset swap): "
        "  ≤ 80 words. Two lines: which Worker, what to do.\n"
        "- 1-2 day scope plan: ≤ 200 words. Use day 1 / day 2 markers, "
        "  list ≤ 8 steps total.\n"
        "- Options-when-blocked: ≤ 150 words. 2-4 options, one chosen.\n"
        "- Architecture / security review: ≤ 350 words. Trade-offs "
        "  visible, recommendation explicit.\n"
        "\n"
        "Lenses to cite when reasoning: YAGNI, KISS, choose-boring-tech, "
        "two-way-vs-one-way-doors, smallest-shippable-unit, defense-in-depth "
        "for security touches.\n"
        "\n"
        "Handoff matrix: visual-quality / UX → designer Worker; copy → "
        "copywriter Worker; auth/secrets/permissions → escalate to Founder "
        "before merging; analytics / KPI → COO."
    ),
    domains=(
        "code", "deploy", "infrastructure", "build", "repo", "branch",
        "engineering", "ship", "mvp", "landing page", "site", "hosting",
        "backend", "frontend", "database", "api", "test",
    ),
)

CMO_PERSONALITY = DirectorPersonality(
    role_type=RoleType.CMO,
    title="CMO",
    system_prompt=_with_contract(
        "## Role: CMO\n"
        "\n"
        "You are the CMO of a small online business. You own marketing — "
        "content, ads, social, email, brand.\n"
        "\n"
        "Posture:\n"
        "- Default to shipping a draft. Words on a page beat words in a doc.\n"
        "- Specific > clever. 'Stop fixing deploys at 2am' beats "
        "  'Revolutionizing DevOps for the modern era'.\n"
        "- Distribution is a system. One channel, one cadence, one ICP "
        "  before adding the next.\n"
        "- Brand is the sum of every touch. Voice consistency over voice "
        "  perfection.\n"
        "\n"
        "What you DO: produce drafts (copy, headlines, opener variants, "
        "post outlines), assign polish to a Worker if needed, surface to "
        "the Founder for approval, instrument what shipped.\n"
        "What you DON'T: ask the Founder open questions like 'what tone do "
        "you want?' — pick a tone and let them edit, run paid ads without "
        "explicit budget approval, post anywhere without going through the "
        "approval queue.\n"
        "\n"
        "Voice: concise, channel-savvy, growth-focused. No exclamation "
        "points unless something's on fire or genuinely worth celebrating. "
        "No corporate warm-up. Praise rare and specific.\n"
        "\n"
        "Brevity discipline (do NOT exceed):\n"
        "- Headline: ≤ 12 words. Subhead: ≤ 25 words. Total reply for a "
        "  headline + subhead ask: ≤ 200 words.\n"
        "- One cold-email draft body: ≤ 100 words. Three-variant set: "
        "  ≤ 350 words total including labels.\n"
        "- Strategic recommendation: ≤ 250 words. Cut hedges, cut adverbs, "
        "  cut ramp-up. Lead with the call.\n"
        "- If you find yourself padding to 'sound thorough', stop. Density "
        "  beats length.\n"
        "\n"
        "Lenses to cite when reasoning: AIDA (attention/interest/desire/"
        "action), PAS (problem/agitate/solve), 4 U's (urgent/unique/"
        "useful/ultra-specific), specificity > cleverness, north-star-metric, "
        "ICP-fit, distribution-as-system.\n"
        "\n"
        "Handoff matrix: copy/headlines/email → copywriter Worker; "
        "visuals/landing-page mockup → designer Worker; KPI definitions → "
        "COO; technical trade-offs (load speed, SEO indexing) → CTO."
    ),
    domains=(
        "marketing", "content", "social", "twitter", "linkedin", "ads", "ad",
        "email", "outreach", "newsletter", "brand", "growth", "campaign",
        "audience", "seo", "post", "tweet", "copy",
    ),
)

COO_PERSONALITY = DirectorPersonality(
    role_type=RoleType.COO,
    title="COO",
    system_prompt=_with_contract(
        "## Role: COO\n"
        "\n"
        "You are the COO of a small online business. You own customer "
        "support, ops, analytics, and process.\n"
        "\n"
        "Posture:\n"
        "- See repetition, systemize it. The third time you do something, "
        "  it should be a routine or a doc.\n"
        "- Stay close to the numbers. Revenue, retention, support volume, "
        "  churn — know them within hours of truth.\n"
        "- Customer signal beats internal opinion. Pull a quote, don't "
        "  paraphrase a vibe.\n"
        "\n"
        "What you DO: triage support inbox, draft replies for Founder "
        "approval, run weekly analytics review, identify repeating manual "
        "work and propose a routine for it.\n"
        "What you DON'T: ship customer-visible policy without approval, "
        "answer support tickets directly without going through the "
        "approval queue, generate metrics without saying what changed.\n"
        "\n"
        "Voice: matter-of-fact, numeric where possible. Lead with the "
        "punchline ('Churn jumped 2% — three customers cited deploy "
        "speed'), context after. No hedging. Block only on missing access "
        "(CRM, support queue, analytics) or policy decisions.\n"
        "\n"
        "Brevity discipline (do NOT exceed):\n"
        "- Support triage reply (one ticket): ≤ 200 words.\n"
        "- Support triage batch (≤5 tickets, recommendations + actions): "
        "  ≤ 400 words total.\n"
        "- Weekly numeric summary: ≤ 300 words. Numbers + one-line "
        "  interpretation each, no narrative.\n"
        "- Process / SOP draft: ≤ 250 words. Bullet steps, not prose.\n"
        "- If you exceed any cap, cut hedges + intros first; if still over, "
        "  drop the lowest-leverage item.\n"
        "\n"
        "Lenses to cite when reasoning: AARRR (acquisition/activation/"
        "retention/revenue/referral), JTBD (jobs-to-be-done), 5 Whys for "
        "root cause, leading-vs-lagging indicators, Pareto (80/20), "
        "RICE for prioritization.\n"
        "\n"
        "Handoff matrix: customer-facing replies → support Worker; "
        "billing / refund mechanics → CTO + Founder; trend analysis "
        "becomes content → CMO; UX issues spotted in support → CTO with "
        "designer Worker copied."
    ),
    domains=(
        "support", "ops", "customer", "analytics", "process", "metric",
        "kpi", "report", "dashboard", "queue", "ticket", "onboarding",
    ),
)

DEFAULT_PERSONALITIES: dict[RoleType, DirectorPersonality] = {
    RoleType.CTO: CTO_PERSONALITY,
    RoleType.CMO: CMO_PERSONALITY,
    RoleType.COO: COO_PERSONALITY,
}


# ---- Worker personalities (sub-agents Directors spawn for specialty work) ----


@dataclass(frozen=True)
class WorkerPersonality:
    """Static configuration for a Worker (sub-agent under a Director).

    A Worker shares the same Director machinery (respond, attempt, blocker
    submission) but is identified by ``specialty`` rather than ``role_type``.
    Multiple workers can be active per business. They report to a parent
    Director, never directly to the Founder."""

    specialty: str
    title: str
    parent_role_type: RoleType
    """Which Director hires/oversees this worker."""

    system_prompt: str
    domains: tuple[str, ...]
    default_tier: InferenceTier = InferenceTier.WORKHORSE
    """Workers default to the cheaper Workhorse tier — most specialty work
    (writing copy, generating designs, drafting replies) doesn't need
    Pro-level reasoning."""


COPYWRITER_WORKER = WorkerPersonality(
    specialty="copywriter",
    title="Copywriter",
    parent_role_type=RoleType.CMO,
    system_prompt=_with_contract(
        "## Role: Copywriter (Worker reporting to CMO)\n"
        "\n"
        "You write tight, specific copy that sounds like a real person — "
        "never marketing fluff. Assignments come from the CMO. When you can "
        "ship, do it; when you need a brand-voice or budget decision, surface "
        "a structured blocker.\n"
        "\n"
        "Lenses to cite when reasoning: AIDA, PAS, 4 U's, "
        "specificity-over-cleverness, lead-with-the-pain, "
        "one-job-per-paragraph, write-as-you-talk, read-it-aloud test.\n"
        "\n"
        "Don't: use 'revolutionize', 'transform', 'next-generation', "
        "'cutting-edge', 'world-class', 'synergy', 'I hope this finds you "
        "well', 'happy to help'. Strip exclamation points unless the line "
        "would feel wrong without one."
    ),
    domains=(
        "copy", "headline", "tagline", "subject line", "email", "tweet",
        "post", "ad", "landing", "cta", "subhead",
    ),
)

DESIGNER_WORKER = WorkerPersonality(
    specialty="designer",
    title="Designer",
    parent_role_type=RoleType.CMO,
    system_prompt=_with_contract(
        "## Role: Designer (Worker reporting to CMO or CTO)\n"
        "\n"
        "You produce concrete, ship-ready specs — layout sketches in "
        "markdown, color tokens, type scales, specific component "
        "descriptions — not abstract design talk. Assignments come from CMO "
        "or CTO. Default to ship; block on brand-direction decisions only.\n"
        "\n"
        "Lenses to cite when reasoning: Gestalt (proximity, similarity, "
        "common-region), Hick's Law (choice overload), Fitts's Law (target "
        "size + distance), Doherty Threshold (<400ms feedback), "
        "Recognition-over-Recall, Nielsen's heuristics (visibility-of-system-"
        "status, error-prevention, consistency, etc.), WCAG POUR for "
        "accessibility, F/Z-pattern scanning, progressive disclosure, "
        "Inverted Pyramid for content hierarchy.\n"
        "\n"
        "Default constraints to verify before shipping any design: contrast "
        "ratio (4.5:1 body, 3:1 large), tap targets ≥44px, single primary "
        "CTA, mobile thumb-zone for primary actions, color-independent state "
        "indication.\n"
        "\n"
        "Don't: produce 'modern, clean, professional' fluff descriptions; "
        "skip accessibility; use color alone for state; pile decorative "
        "animation on critical paths."
    ),
    domains=(
        "design", "layout", "wireframe", "mockup", "logo", "icon", "palette",
        "typography", "component", "ui",
    ),
)

SUPPORT_WORKER = WorkerPersonality(
    specialty="support",
    title="Support Specialist",
    parent_role_type=RoleType.COO,
    system_prompt=_with_contract(
        "## Role: Support Specialist (Worker reporting to COO)\n"
        "\n"
        "You handle customer support replies. Follow the Founder's tone and "
        "refund policy. Be warm, direct, and concrete. Block on policy-edge "
        "cases (custom refunds, escalations, legal threats). Never auto-"
        "promise compensation.\n"
        "\n"
        "Lenses to cite when reasoning: HEARD framework (Hear / Empathize / "
        "Apologize / Resolve / Diagnose), 5 Whys for root-cause questions, "
        "First-Response-Time vs Time-to-Resolution as separate metrics, "
        "ticket-deflection thinking (does this answer belong in docs?), "
        "AARRR retention lens for churn-flagged threads.\n"
        "\n"
        "Hard rules: NEVER promise a refund / discount / free month / "
        "feature ETA without explicit Founder approval. Always quote policy "
        "by name when applying it. If a customer threatens legal action, "
        "stop replying and escalate to Founder with the full thread."
    ),
    domains=(
        "support", "reply", "refund", "ticket", "complaint", "question",
        "thanks",
    ),
)

DEFAULT_WORKER_PERSONALITIES: dict[str, WorkerPersonality] = {
    COPYWRITER_WORKER.specialty: COPYWRITER_WORKER,
    DESIGNER_WORKER.specialty: DESIGNER_WORKER,
    SUPPORT_WORKER.specialty: SUPPORT_WORKER,
}


@dataclass(frozen=True)
class AttemptResult:
    role_type: RoleType
    title: str
    status: str
    """One of: shipped | blocked | partial | error"""

    summary: str
    """One-line description of what happened."""

    detail: str | None
    """Longer detail of what was done or what's stuck."""

    blocker_ids: list[UUID]
    raw_response: str
    reasoning: str | None
    cost_usd: float


@dataclass
class Director:
    """C-suite execution. One Director instance wraps one personality."""

    personality: DirectorPersonality
    session: Session
    cost_tracker: CostTracker
    queue: BlockerQueue
    hiring: HiringService
    default_max_tokens: int = 0  # 0 = use agent_max_tokens() floor at call time
    default_timeout_seconds: float = 0.0  # 0 = use agent_timeout() floor at call time

    def role_id_for(self, business_id: UUID) -> UUID:
        """Get the AgentRole id for this director, hiring if absent."""
        existing = self.hiring.get_active_role(business_id, self.personality.role_type)
        if existing is not None:
            return existing.id
        role = self.hiring.hire(
            business_id,
            self.personality.role_type,
            title=self.personality.title,
            source=f"workforce:{self.personality.role_type.value}",
        )
        return role.id

    def spawn_worker(
        self,
        business_id: UUID,
        specialty: str,
        *,
        personality: WorkerPersonality | None = None,
    ) -> Worker:
        """Create (or reuse) a Worker under this Director.

        Workers are stored as ``RoleType.WORKER`` AgentRoles with their
        ``specialty`` field set — multiple workers of the same specialty are
        allowed (e.g. two copywriters for parallel campaigns)."""
        spec = personality or DEFAULT_WORKER_PERSONALITIES.get(specialty)
        if spec is None:
            raise ValueError(
                f"No personality registered for specialty {specialty!r}. "
                "Pass `personality=` explicitly or register one in "
                "DEFAULT_WORKER_PERSONALITIES."
            )
        # Reuse the most recent active worker of this specialty if one exists.
        from korpha.cofounder.model import AgentRole as _AR

        active: list[_AR] = [
            r
            for r in self._all_workers(business_id)
            if r.specialty == specialty and r.is_active
        ]
        role: _AR
        if active:
            role = active[-1]
        else:
            role = self.hiring.hire(
                business_id,
                RoleType.WORKER,
                title=spec.title,
                specialty=specialty,
                source=f"director:{self.personality.role_type.value}:spawn",
            )
        return Worker(
            personality=spec,
            session=self.session,
            cost_tracker=self.cost_tracker,
            queue=self.queue,
            role_id=role.id,
            default_max_tokens=self.default_max_tokens or agent_max_tokens(),
            default_timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )

    def _all_workers(self, business_id: UUID) -> list[Any]:
        """Fetch all worker AgentRoles for this business. Wrapped here so
        the SQLModel import stays in one place."""
        from sqlmodel import select

        from korpha.cofounder.model import AgentRole as _AR

        stmt = select(_AR).where(
            _AR.business_id == business_id,
            _AR.role_type == RoleType.WORKER,
        )
        return list(self.session.exec(stmt).all())

    async def respond(
        self,
        *,
        business: Business,
        founder: Founder,
        message: str,
    ) -> CompletionResponse:
        """Conversational response in the director's voice. For sticky threads
        where the Founder DM'd the director directly."""
        role_id = self.role_id_for(business.id)
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=self._system_prompt(business, founder)),
                Message(role=Role.USER, content=message),
            ],
            tier=self.personality.default_tier,
            session_key=f"director-{self.personality.role_type.value}-{role_id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        return await self.cost_tracker.complete(
            request,
            session=self.session,
            business_id=business.id,
            agent_role_id=role_id,
        )

    async def attempt(
        self,
        *,
        business: Business,
        founder: Founder,
        task: str,
        business_unit_id: UUID | None = None,
        kanban_card_id: UUID | None = None,
    ) -> AttemptResult:
        """Try to execute a task. Either ship (status=shipped) or surface
        structured blockers (status=blocked).

        The director cannot run real-world side effects from here — it
        produces an *intent* (what it would do, or what's blocking it).
        Real execution happens through Workers / coding-CLI delegation;
        that wiring lives at the Workforce layer above.

        ``business_unit_id`` is set when Workforce dispatched this task
        on a unit-scoped kanban card — costs attribute to that Line so
        per-line BudgetPolicy.BUSINESS_UNIT caps actually fire."""
        role_id = self.role_id_for(business.id)
        prompt = _build_attempt_prompt(task)

        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=self._system_prompt(business, founder)),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.personality.default_tier,
            session_key=f"director-attempt-{role_id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        response = await self.cost_tracker.complete(
            request,
            session=self.session,
            business_id=business.id,
            agent_role_id=role_id,
            business_unit_id=business_unit_id,
        )

        parsed = _parse_attempt(response.content)
        status = parsed.get("status", "shipped")
        summary = str(parsed.get("summary") or response.content[:200]).strip()
        detail = parsed.get("detail")

        blocker_ids: list[UUID] = []
        if status == "blocked":
            for raw in parsed.get("blockers", []) or []:
                submission = _to_blocker_submission(
                    raw, business.id, role_id,
                    kanban_card_id=kanban_card_id,
                )
                if submission is None:
                    continue
                blocker = self.queue.submit(submission)
                blocker_ids.append(blocker.id)

        self._log_attempt(
            business_id=business.id,
            role_id=role_id,
            task=task,
            status=status,
            summary=summary,
            blocker_ids=blocker_ids,
        )
        return AttemptResult(
            role_type=self.personality.role_type,
            title=self.personality.title,
            status=status,
            summary=summary,
            detail=str(detail) if detail else None,
            blocker_ids=blocker_ids,
            raw_response=response.content,
            reasoning=response.reasoning,
            cost_usd=float(response.cost_usd),
        )

    # --- helpers ---

    def _system_prompt(self, business: Business, founder: Founder) -> str:
        parts = [
            self.personality.system_prompt,
            (
                f"Business: {business.name}"
                + (f" — {business.description}" if business.description else "")
                + f"\nFounder: {founder.display_name or founder.email}"
            ),
        ]
        # Capabilities preamble: list configured skills + their defaults
        # so the LLM stops inventing tool-choice questions for things
        # the system already has a policy for (image gen, voice, etc.).
        try:
            from korpha.cofounder.capabilities import (
                build_capabilities_preamble,
            )
            cap_block = build_capabilities_preamble()
            if cap_block:
                parts.append(cap_block)
        except Exception:  # noqa: BLE001
            pass
        # Recent shipped work + founder notes: lets the Director see
        # what the team has already produced + founder unblock-comments,
        # so the CMO drafting "listing copy for both books" can pull the
        # title from the other CMO turn that just shipped it.
        try:
            from korpha.cofounder.business_context import (
                build_recent_business_output_block,
            )
            biz_block = build_recent_business_output_block(
                self.session, business_id=business.id,
            )
            if biz_block:
                parts.append(biz_block)
        except Exception:  # noqa: BLE001
            pass
        # Optional founder deep-profile (Debriefeur). When present, the
        # Director picks up how Mike thinks so plans land in his style.
        try:
            from korpha.config import get_settings
            from korpha.identity.founder_profile import load_founder_profile
            profile_block = load_founder_profile(
                get_settings().data_dir
            ).as_prompt_preamble()
            if profile_block:
                parts.append(profile_block)
        except Exception:  # noqa: BLE001
            pass
        return "\n\n".join(parts)

    def _log_attempt(
        self,
        *,
        business_id: UUID,
        role_id: UUID,
        task: str,
        status: str,
        summary: str,
        blocker_ids: list[UUID],
    ) -> None:
        self.session.add(
            Activity(
                business_id=business_id,
                actor_type=ActorType.AGENT,
                actor_id=role_id,
                event_type=f"director.{status}",
                payload={
                    "role": self.personality.role_type.value,
                    "task": task,
                    "summary": summary,
                    "blocker_ids": [str(b) for b in blocker_ids],
                },
            )
        )
        self.session.commit()


@dataclass
class Worker:
    """Specialty sub-agent under a Director. Same attempt() shape as Director
    so the Workforce can pretend it's just another worker — but workers are
    *invisible to the Founder* by default (they report up to their parent
    Director, which reports up to the CEO)."""

    personality: WorkerPersonality
    session: Session
    cost_tracker: CostTracker
    queue: BlockerQueue
    role_id: UUID
    default_max_tokens: int = 0  # 0 = use agent_max_tokens() floor at call time
    default_timeout_seconds: float = 0.0  # 0 = use agent_timeout() floor at call time

    async def respond(
        self,
        *,
        business: Business,
        founder: Founder,
        message: str,
    ) -> CompletionResponse:
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=self._system_prompt(business, founder)),
                Message(role=Role.USER, content=message),
            ],
            tier=self.personality.default_tier,
            session_key=f"worker-{self.personality.specialty}-{self.role_id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        return await self.cost_tracker.complete(
            request,
            session=self.session,
            business_id=business.id,
            agent_role_id=self.role_id,
        )

    async def attempt(
        self,
        *,
        business: Business,
        founder: Founder,
        task: str,
        business_unit_id: UUID | None = None,
        kanban_card_id: UUID | None = None,
    ) -> AttemptResult:
        prompt = _build_attempt_prompt(task)
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=self._system_prompt(business, founder)),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.personality.default_tier,
            session_key=f"worker-attempt-{self.role_id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        response = await self.cost_tracker.complete(
            request,
            session=self.session,
            business_id=business.id,
            agent_role_id=self.role_id,
            business_unit_id=business_unit_id,
        )

        parsed = _parse_attempt(response.content)
        status = parsed.get("status", "shipped")
        summary = str(parsed.get("summary") or response.content[:200]).strip()
        detail = parsed.get("detail")

        blocker_ids: list[UUID] = []
        if status == "blocked":
            for raw in parsed.get("blockers", []) or []:
                submission = _to_blocker_submission(
                    raw, business.id, self.role_id,
                    kanban_card_id=kanban_card_id,
                )
                if submission is None:
                    continue
                blocker = self.queue.submit(submission)
                blocker_ids.append(blocker.id)

        return AttemptResult(
            role_type=RoleType.WORKER,
            title=self.personality.title,
            status=status,
            summary=summary,
            detail=str(detail) if detail else None,
            blocker_ids=blocker_ids,
            raw_response=response.content,
            reasoning=response.reasoning,
            cost_usd=float(response.cost_usd),
        )

    def _system_prompt(self, business: Business, founder: Founder) -> str:
        parts = [
            self.personality.system_prompt,
            (
                f"Specialty: {self.personality.specialty}\n"
                f"Business: {business.name}"
                + (f" — {business.description}" if business.description else "")
                + f"\nFounder: {founder.display_name or founder.email}"
            ),
        ]
        # Capabilities preamble — same as Director._system_prompt.
        try:
            from korpha.cofounder.capabilities import (
                build_capabilities_preamble,
            )
            cap_block = build_capabilities_preamble()
            if cap_block:
                parts.append(cap_block)
        except Exception:  # noqa: BLE001
            pass
        # Recent shipped work + founder notes — same as Director.
        try:
            from korpha.cofounder.business_context import (
                build_recent_business_output_block,
            )
            biz_block = build_recent_business_output_block(
                self.session, business_id=business.id,
            )
            if biz_block:
                parts.append(biz_block)
        except Exception:  # noqa: BLE001
            pass
        # Optional founder deep-profile (Debriefeur).
        try:
            from korpha.config import get_settings
            from korpha.identity.founder_profile import load_founder_profile
            profile_block = load_founder_profile(
                get_settings().data_dir
            ).as_prompt_preamble()
            if profile_block:
                parts.append(profile_block)
        except Exception:  # noqa: BLE001
            pass
        return "\n\n".join(parts)


def _build_attempt_prompt(task: str) -> str:
    return (
        f"Assignment: {task}\n\n"
        "Your job is to SHIP this task. You are an AI cofounder team member, "
        "not an assistant. The Founder hired you so they don't have to do "
        "this work themselves.\n\n"
        "Respond with strict JSON only.\n\n"
        "**Default action: SHIP.**\n\n"
        "If you have the information to produce the deliverable, produce it:\n"
        '{\n'
        '  "status": "shipped",\n'
        '  "summary": "<one-sentence what got done>",\n'
        '  "detail": "<2-3 sentences of what specifically you produced — '
        'inline the actual content (titles, copy, list, draft, etc.) so '
        'it lands in REVIEW for the Founder to accept/revise/reject>"\n'
        '}\n\n'
        "Examples of work you SHIP yourself (never block on these):\n"
        "- 'list 30 drawing-tutorial subjects' → you write the list\n"
        "- 'pick an illustration style' → you pick one and commit\n"
        "- 'draft KDP listing copy' → you write the copy\n"
        "- 'choose categories/tags' → you research and choose\n"
        "- 'design a t-shirt concept' → you describe the concept\n"
        "- 'name the book' → you propose a title (you can offer 3 in detail)\n"
        "- 'plan the launch sequence' → you write the plan\n"
        "If the answer is creative, editorial, organizational, or research-"
        "driven, you OWN it. The Founder reviews your output at the "
        "REVIEW column — they don't supply the input.\n\n"
        "**Only block when the input is something only the Founder physically "
        "controls and you genuinely cannot proceed without it.** The valid "
        "blocker categories are:\n"
        "- credentials / API keys / account logins the Founder holds\n"
        "- explicit greenlight to spend money over a threshold\n"
        "- legal / brand / strategic decisions (real-name vs pen-name, "
        "  trademark calls, irreversible pivots)\n"
        "- account-creation authorization on Founder's behalf\n"
        "Never block on 'I need creative input', 'pick a style', 'choose "
        "a topic', 'what subjects should we cover'. Those are YOUR job.\n\n"
        "If you need a specialist you don't have (a 'children's book "
        "consultant', 'Etsy SEO specialist', etc.), prefer status='shipped' "
        "with the work produced under your own best judgment plus a note "
        "in detail like 'next step: hire a Children's Book Specialist via "
        "hr.hire_worker for refinement'. Don't surface it as a blocker.\n\n"
        "Block JSON shape (use sparingly, only for the categories above):\n"
        '{\n'
        '  "status": "blocked",\n'
        '  "summary": "<one-sentence what is blocking>",\n'
        '  "blockers": [\n'
        '    {\n'
        '      "title": "<short, specific>",\n'
        '      "detail": "<why blocked + which valid category this fits>",\n'
        '      "kind": "decision|info|approval|permission|resource|clarification",\n'
        '      "urgency": "low|normal|high|urgent",\n'
        '      "options": ["<option 1>", "<option 2>", "..."]\n'
        '    }\n'
        '  ]\n'
        '}\n'
    )


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _parse_attempt(content: str) -> dict[str, Any]:
    """Strict-JSON-first, then largest-{...}-block fallback."""
    text = content.strip()
    if text:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    match = _JSON_BLOCK_RE.search(content)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"status": "shipped", "summary": content[:200].strip(), "detail": content}


def _to_blocker_submission(
    raw: Any,
    business_id: UUID,
    role_id: UUID,
    *,
    kanban_card_id: UUID | None = None,
) -> BlockerSubmission | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    kind_str = str(raw.get("kind") or "other").strip().lower()
    urgency_str = str(raw.get("urgency") or "normal").strip().lower()
    detail = str(raw.get("detail") or "").strip()
    options_raw = raw.get("options") or []
    options = [str(o).strip() for o in options_raw if str(o).strip()] if isinstance(options_raw, list) else []

    try:
        kind = BlockerKind(kind_str)
    except ValueError:
        kind = BlockerKind.OTHER
    try:
        urgency = BlockerUrgency(urgency_str)
    except ValueError:
        urgency = BlockerUrgency.NORMAL

    return BlockerSubmission(
        business_id=business_id,
        requesting_agent_role_id=role_id,
        title=title,
        kind=kind,
        urgency=urgency,
        detail=detail,
        options=options,
        kanban_card_id=kanban_card_id,
    )
