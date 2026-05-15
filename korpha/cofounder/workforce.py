"""Workforce — orchestrates Directors against a Plan.

Picks the right Director for each task by keyword-matching the task against
each Director's `domains`. Dispatches assignments in parallel via asyncio.
Collects AttemptResults so the CEO can summarize for the Founder.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from uuid import UUID

from korpha.audit.model import Activity, ActorType
from korpha.business.model import Business
from korpha.cofounder.director import (
    DEFAULT_PERSONALITIES,
    AttemptResult,
    Director,
    DirectorPersonality,
)
from korpha.cofounder.model import RoleType
from korpha.identity.model import Founder


# Process-wide registry of currently-running director attempts so the
# TUI's ``subagent.interrupt`` RPC method can cancel just one role's
# task without killing the whole prompt.submit. Key: (business_id_str,
# role_type_str). Value: the asyncio Task running that director's
# attempt(). Not thread-safe — relies on the single asyncio loop the
# server runs on.
_SUBAGENT_TASKS: dict[tuple[str, str], asyncio.Task[AttemptResult]] = {}


def list_running_subagents() -> list[dict[str, str]]:
    """Snapshot of every director attempt currently in flight. The
    TUI ``subagent.list`` RPC reads this. Returns a fresh list each
    call so callers can iterate safely while tasks come and go."""
    return [
        {"business_id": bid, "role_type": role}
        for (bid, role), task in list(_SUBAGENT_TASKS.items())
        if not task.done()
    ]


def cancel_subagent(business_id: str, role_type: str) -> bool:
    """Cancel the running director attempt for this (business, role)
    pair. Returns True if a task was cancelled, False if nothing was
    running for that pair. Cooperative — the asyncio task gets
    cancelled, which raises CancelledError inside the LLM call's
    ``await``; the Workforce wrapper turns that into an AttemptResult
    with status='blocked' so the CEO can summarize."""
    key = (str(business_id), str(role_type).lower())
    task = _SUBAGENT_TASKS.get(key)
    if task is None or task.done():
        return False
    task.cancel()
    return True


@dataclass
class Workforce:
    """Holds the configured directors and routes tasks to them."""

    directors: dict[RoleType, Director]
    fallback_role: RoleType = RoleType.CTO
    """Used when no director's domains match a task. CTO is a reasonable
    default since most ambiguous tasks lean implementation-y."""

    def select_executor(
        self, task: str, *, business_id: UUID,
    ):
        """Pick whoever handles this task — Director or Worker.

        Routing precedence:

          1. ``[WORKER:specialty]`` tag → spawn/reuse a Worker of
             that specialty under the natural parent Director (CMO
             owns copywriter/designer; CTO owns engineering
             workers; COO owns support workers). Falls back to the
             role tag's Director if no worker personality is
             registered for that specialty.
          2. ``[CTO]`` / ``[CMO]`` / ``[COO]`` tag → that Director.
          3. Keyword scoring against Director.domains.
          4. Fallback role.

        Returns either a Director or a Worker — both expose the
        same ``attempt()`` shape so the dispatcher doesn't care."""
        # 1. Worker tag wins above everything else
        worker_specialty = _worker_specialty_from_tag(task)
        if worker_specialty is not None:
            spawned = self._spawn_worker_by_specialty(
                worker_specialty, business_id=business_id,
            )
            if spawned is not None:
                return spawned
            # No worker personality registered → fall through to
            # the natural-parent Director so the task still runs
            # (with C-suite quality, no specialty boost).

        # 2 & 3 & 4 — same as before
        return self.select_director(task)

    def _spawn_worker_by_specialty(
        self, specialty: str, *, business_id: UUID,
    ):
        """Find the right parent Director for ``specialty`` and
        ask it to spawn (or reuse) a Worker. Returns None when no
        Director knows how to spawn that specialty (no personality
        registered)."""
        from korpha.cofounder.director import (
            DEFAULT_WORKER_PERSONALITIES,
        )

        spec = DEFAULT_WORKER_PERSONALITIES.get(specialty)
        if spec is None:
            return None
        parent_role = spec.parent_role_type
        parent = self.directors.get(parent_role)
        if parent is None:
            return None
        try:
            return parent.spawn_worker(business_id, specialty)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "worker spawn failed for specialty=%r", specialty,
                exc_info=True,
            )
            return None

    def _lookup_card_unit_id(
        self, task: str, *, business_id: UUID, session,
    ) -> UUID | None:
        """Find the BusinessUnit a kanban card belongs to (if any).
        Returns the card's ``business_unit_id`` so Director / Worker
        cost rows can attribute to the Line for per-unit budget caps.
        Returns None when no matching card / no unit on the card."""
        if session is None:
            return None
        try:
            from sqlmodel import Session as _S
            from sqlmodel import select as _select
            from korpha.kanban.model import KanbanCard, KanbanColumn
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(session, _S):
            return None
        title = _strip_role_tag(task)
        if not title:
            return None
        try:
            cards = list(session.exec(
                _select(KanbanCard)
                .where(KanbanCard.business_id == business_id)
                .where(KanbanCard.title == title)
                .where(KanbanCard.column == KanbanColumn.BACKLOG)
            ).all())
        except Exception:  # noqa: BLE001
            return None
        if not cards:
            return None
        cards.sort(key=lambda c: c.created_at, reverse=True)
        return cards[0].business_unit_id

    def _select_unit_vp_executor(
        self, task: str, *, business_id: UUID, session,
    ):
        """PR-INT-15: route a kanban-mirrored task through its
        line's VP when the card has business_unit_id set.

        Returns a ``VpExecutor`` quacking like a Director, or None
        when no card matches / no unit context applies.

        Failures here MUST fall through to the regular Director
        path so a malformed card doesn't block the dispatch loop.
        """
        if session is None:
            return None
        try:
            from sqlmodel import Session as _S
            from sqlmodel import select as _select

            from korpha.cofounder.vp_runner import VpExecutor
            from korpha.business_units.model import BusinessUnit
            from korpha.kanban.model import KanbanCard, KanbanColumn
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(session, _S):
            return None

        title = _strip_role_tag(task)
        if not title:
            return None
        try:
            candidates = list(session.exec(
                _select(KanbanCard)
                .where(KanbanCard.business_id == business_id)
                .where(KanbanCard.title == title)
                .where(KanbanCard.column == KanbanColumn.BACKLOG)
            ).all())
        except Exception:  # noqa: BLE001
            return None
        if not candidates:
            return None
        candidates.sort(key=lambda c: c.created_at, reverse=True)
        card = candidates[0]
        if card.business_unit_id is None:
            return None
        unit = session.get(BusinessUnit, card.business_unit_id)
        if unit is None or unit.owner_agent_role_id is None:
            return None

        # Pull a CostTracker from any registered director
        cost_tracker = next(
            (d.cost_tracker for d in self.directors.values()), None,
        )
        if cost_tracker is None:
            return None
        return VpExecutor(
            unit_id=card.business_unit_id,
            session=session,
            cost_tracker=cost_tracker,
        )

    def select_director(self, task: str) -> Director:
        # Explicit role tag always wins. CEO prompt asks for [CTO]/[CMO]/[COO]
        # prefixes — when present, trust them.
        tagged = self._role_from_tag(task)
        if tagged is not None and tagged in self.directors:
            return self.directors[tagged]

        text = task.lower()
        # Score each director by word-boundary keyword matches. Substring
        # matching collides badly ("ad" matches "carrd"/"automation"), so we
        # require whole-word hits. Multi-word keywords like "landing page"
        # are matched as exact phrase substrings since they have their own
        # boundaries already.
        scored: list[tuple[int, RoleType, Director]] = []
        for role, director in self.directors.items():
            score = 0
            for kw in director.personality.domains:
                if " " in kw:
                    if kw in text:
                        score += 1
                elif _word_re(kw).search(text):
                    score += 1
            if score > 0:
                scored.append((score, role, director))
        if scored:
            scored.sort(key=lambda x: (-x[0], x[1].value))
            return scored[0][2]
        return self.directors[self.fallback_role]

    @staticmethod
    def _role_from_tag(task: str) -> RoleType | None:
        """Detect leading [CTO] / [CMO] / [COO] tag in the task."""
        match = _ROLE_TAG_RE.match(task.lstrip())
        if match is None:
            return None
        try:
            return RoleType(match.group(1).lower())
        except ValueError:
            return None

    async def dispatch(
        self,
        *,
        business: Business,
        founder: Founder,
        tasks: list[str],
    ) -> list[AttemptResult]:
        """Dispatch each task to the matching Director and run them in
        parallel. Returns one AttemptResult per task (in input order).

        Per-director cancel: each attempt runs in its own asyncio task
        registered in ``_SUBAGENT_TASKS`` so the TUI's
        ``subagent.interrupt`` RPC can cancel just one role without
        killing the whole prompt.submit. Cancellation is cooperative —
        we catch CancelledError + return a blocked AttemptResult so
        the CEO can summarize cleanly.

        Kanban: when the CEO mirrored Plan tasks onto the board (in
        BACKLOG), we auto-advance the matching card to IN_PROGRESS at
        dispatch start and to REVIEW with the AttemptResult summary as
        evidence at dispatch end. The board is the visible record of
        what the workforce did this turn.
        """
        if not tasks:
            return []

        # Resolve a director.session to use for kanban bookkeeping.
        # Workforce doesn't carry its own session, so we borrow the
        # first director's. All directors share the same session
        # (DirectorFactory wires them that way), so this is safe.
        kanban_session = next(
            (d.session for d in self.directors.values()), None,
        )
        kanban_handles: list[KanbanHandle | None] = []

        async_tasks: list[asyncio.Task[AttemptResult]] = []
        for task_text in tasks:
            # PR-INT-15: when a matching kanban card already exists +
            # has business_unit_id set + the unit has an owner agent,
            # route through that unit's VP instead of the generic
            # director. The VP runs in the unit's namespace so memory/
            # cooperation calls auto-scope.
            executor = self._select_unit_vp_executor(
                task_text, business_id=business.id,
                session=kanban_session,
            ) or self.select_executor(
                task_text, business_id=business.id,
            )
            # Even without a VP, surface the card's unit so Director/Worker
            # costs attribute to the Line for BUSINESS_UNIT budget caps.
            card_unit_id = self._lookup_card_unit_id(
                task_text, business_id=business.id, session=kanban_session,
            )
            # Workers carry personality.specialty + parent_role_type;
            # Directors carry personality.role_type. The kanban
            # bookkeeping owner is the parent role for workers
            # (so the card claim links to the supervising
            # Director's AgentRole, not the worker's transient
            # role) — that keeps /app/kanban legible.
            from korpha.cofounder.director import Worker as _Worker
            if isinstance(executor, _Worker):
                kanban_role = executor.personality.parent_role_type
                key_role = (
                    f"worker:{executor.personality.specialty}"
                )
            else:
                kanban_role = executor.personality.role_type
                key_role = kanban_role.value.lower()

            key = (str(business.id), key_role)
            handle = _kanban_advance_to_in_progress(
                session=kanban_session,
                business_id=business.id,
                task_text=task_text,
                role_type=kanban_role,
            )
            kanban_handles.append(handle)
            # VpExecutor.attempt doesn't accept business_unit_id (it
            # already runs in unit scope from construction); only thread
            # the kwarg for Director/Worker executors.
            from korpha.cofounder.director import Director as _Director
            from korpha.cofounder.director import Worker as _WorkerCls
            if isinstance(executor, (_Director, _WorkerCls)):
                coro = executor.attempt(
                    business=business, founder=founder, task=task_text,
                    business_unit_id=card_unit_id,
                )
            else:
                coro = executor.attempt(
                    business=business, founder=founder, task=task_text,
                )
            async_task = asyncio.create_task(coro)
            _SUBAGENT_TASKS[key] = async_task
            async_tasks.append(async_task)

        try:
            results = await asyncio.gather(
                *async_tasks, return_exceptions=True,
            )
        finally:
            # Always clear our registry entries so the next dispatch
            # starts clean even if something raised.
            for atask in async_tasks:
                # find the key for this task and drop it
                for k, v in list(_SUBAGENT_TASKS.items()):
                    if v is atask:
                        _SUBAGENT_TASKS.pop(k, None)
                        break

        normalized: list[AttemptResult] = []
        for raw, task_text, handle in zip(
            results, tasks, kanban_handles, strict=False,
        ):
            if isinstance(raw, asyncio.CancelledError):
                # Sub-agent was interrupted — surface as blocked so
                # the CEO can decide what to do (try a different
                # approach, drop the task, hand back to founder).
                attempt = _cancelled_result(task_text)
            else:
                attempt = _normalize(raw, task_text)
            normalized.append(attempt)
            _kanban_finalize(
                session=kanban_session,
                handle=handle,
                attempt=attempt,
            )
        return normalized

    @classmethod
    def with_default_directors(
        cls,
        *,
        director_factory: DirectorFactory,
    ) -> Workforce:
        """Build a Workforce with CTO/CMO/COO default personalities."""
        directors = {
            role: director_factory.build(personality=personality)
            for role, personality in DEFAULT_PERSONALITIES.items()
        }
        return cls(directors=directors)


@dataclass(frozen=True)
class KanbanHandle:
    """Reference to the card a director is working this turn. Used
    by ``_kanban_finalize`` to attach evidence + move REVIEW after
    the attempt completes."""

    card_id: UUID


def _strip_role_tag(text: str) -> str:
    """Drop a leading [CTO]/[CMO]/[COO] tag and any trailing punctuation
    so we can match a plan task against the title CEO mirrored to the
    kanban board (the mirror also strips the tag)."""
    cleaned = text.strip()
    if cleaned.startswith("["):
        close = cleaned.find("]")
        if close > 0:
            cleaned = cleaned[close + 1 :].lstrip(" :-")
    return cleaned.strip()


def _kanban_advance_to_in_progress(
    *,
    session: object | None,
    business_id: UUID,
    task_text: str,
    role_type: RoleType,
) -> KanbanHandle | None:
    """Find the kanban card the CEO mirrored for this task and
    advance it BACKLOG → SPECIFY → READY → IN_PROGRESS. Returns a
    handle pointing at the card, or None if no card matched (which
    keeps existing tests + non-mirrored flows working unchanged).

    Failures are caught + logged. The workforce's primary job is to
    run the LLM, not bookkeep — a kanban hiccup must never stop the
    director attempt.
    """
    if session is None:
        return None
    try:
        from sqlmodel import Session as _S
        from sqlmodel import select as _select

        from korpha.kanban import KanbanBoard
        from korpha.kanban.model import KanbanCard, KanbanColumn
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(session, _S):
        return None

    title = _strip_role_tag(task_text)
    if not title:
        return None
    try:
        # We want the most recent matching card. Priority:
        #   1. IN_PROGRESS — already claimed by fire_sprint /
        #      auto-dispatch / cron path. We DON'T re-transition;
        #      just return a handle so the end-of-dispatch
        #      finalize can write review_evidence.
        #   2. BACKLOG — the original CEO-Plan path; the function
        #      auto-specifies + advances through SPECIFY → READY →
        #      IN_PROGRESS as before.
        # Multiple matches → newest wins.
        all_matches = list(session.exec(
            _select(KanbanCard)
            .where(KanbanCard.business_id == business_id)
            .where(KanbanCard.title == title)
            .where(
                KanbanCard.column.in_([  # type: ignore[union-attr]
                    KanbanColumn.IN_PROGRESS,
                    KanbanColumn.BACKLOG,
                ])
            )
        ).all())
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "kanban lookup failed for task %r", title, exc_info=True,
        )
        return None
    if not all_matches:
        return None
    # Sort: IN_PROGRESS first (so we attach to the already-claimed
    # card if one exists), then by created_at desc within each group.
    all_matches.sort(
        key=lambda c: (
            0 if c.column == KanbanColumn.IN_PROGRESS else 1,
            -(c.created_at.timestamp() if c.created_at else 0),
        )
    )
    card = all_matches[0]
    # Already-IN_PROGRESS short-circuit: return a handle without
    # re-running the SPECIFY → READY → CLAIM dance. The end-of-
    # dispatch finalize will write review_evidence to this card.
    if card.column == KanbanColumn.IN_PROGRESS:
        return KanbanHandle(card_id=card.id)

    board = KanbanBoard(session)
    try:
        # Auto-specify with the task as the criterion. The workforce-
        # dispatch flow is implicit-acceptance: the LLM uses the task
        # text as its definition of done; we treat that as the single
        # acceptance criterion. Mike can manually re-specify later.
        if not card.acceptance_criteria:
            board.specify(
                card.id,
                acceptance_criteria=[title],
                owner_role=role_type.value,
            )
        elif card.owner_role is None:
            board.specify(
                card.id,
                acceptance_criteria=card.acceptance_criteria,
                owner_role=role_type.value,
            )
        board.move(card.id, KanbanColumn.READY)
        # Find or create the AgentRole for this role_type so the claim
        # has a real role_id to record. The hiring service does this
        # idempotently; we look it up here without mutating state.
        from korpha.cofounder.model import AgentRole
        role = session.exec(
            _select(AgentRole)
            .where(AgentRole.business_id == business_id)
            .where(AgentRole.role_type == role_type)
            .where(AgentRole.is_active)
        ).first()
        if role is None:
            return None
        board.claim(
            card.id,
            agent_role_id=role.id,
            actor_role=role_type.value,
        )
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "kanban auto-advance failed for card %s",
            card.id, exc_info=True,
        )
        return None
    return KanbanHandle(card_id=card.id)


def _kanban_finalize(
    *,
    session: object | None,
    handle: KanbanHandle | None,
    attempt: AttemptResult,
) -> None:
    """Attach the AttemptResult summary as evidence + move the card
    to REVIEW for human verification. Status='blocked' or 'error'
    cards go back to IN_PROGRESS → READY (release the claim) so a
    different turn can retry. Best-effort; logs + drops on failure."""
    if handle is None or session is None:
        return
    try:
        from sqlmodel import Session as _S

        from korpha.kanban import KanbanBoard
        from korpha.kanban.model import KanbanColumn
    except Exception:  # noqa: BLE001
        return

    if not isinstance(session, _S):
        return

    board = KanbanBoard(session)
    try:
        if attempt.status == "shipped":
            evidence = attempt.summary or "(shipped — no summary)"
            if attempt.detail:
                evidence = f"{evidence}\n\n{attempt.detail}"
            card = board.submit_review_evidence(
                handle.card_id, evidence=evidence,
            )
            # Auto-create a typed artifact mirroring the
            # evidence so /app/kanban renders structured links
            # rather than only the prose blob. We extract the
            # first URL we see, classify it, and tag it primary.
            try:
                _maybe_create_artifact(
                    session, card=card, attempt=attempt,
                )
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "artifact emit failed for %s", card.id,
                    exc_info=True,
                )
        else:
            # blocked / partial / error — release the claim so the
            # board reflects the card is no longer being worked.
            board.move(
                handle.card_id, KanbanColumn.READY,
                note=(
                    f"workforce returned status={attempt.status}: "
                    + (attempt.summary or "")[:200]
                ),
            )
            # Clear the auto_dispatch stamp so the next "go" turn
            # actually re-fires this card. Without this, the 30-min
            # cooldown silently blocks the founder's explicit retry.
            from korpha.kanban.model import KanbanCard
            card_row = session.get(KanbanCard, handle.card_id)
            if card_row is not None:
                meta = dict(card_row.metadata_json or {})
                if meta.pop("auto_dispatch_at", None) is not None:
                    card_row.metadata_json = meta
                    session.add(card_row)
                    session.commit()
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "kanban finalize failed for card %s", handle.card_id,
            exc_info=True,
        )


_ARTIFACT_URL_RE = re.compile(r"https?://[^\s)\]]+")


def _maybe_create_artifact(
    session, *, card, attempt,
) -> None:
    """Auto-emit a typed artifact for a shipped attempt.

    Heuristic classifier:
      * URL contains '/pull/' or '/commit/' → ArtifactKind.PR
      * URL contains '/deployments/' / 'vercel.app' / 'pages.dev'
        / 'netlify.app' → ArtifactKind.DEPLOY
      * any other URL → ArtifactKind.URL
      * no URL → ArtifactKind.OTHER with the summary as label

    First artifact on a card auto-flagged primary so /app/kanban
    shows it as the headline link."""
    from korpha.kanban.artifacts import (
        ArtifactKind, ArtifactService,
    )

    text = " ".join(filter(None, [
        getattr(attempt, "summary", "") or "",
        getattr(attempt, "detail", "") or "",
    ]))
    matches = _ARTIFACT_URL_RE.findall(text)
    svc = ArtifactService(session)
    existing = svc.list_for_card(card.id)
    is_primary = not existing  # first artifact wins primary

    if not matches:
        svc.add(
            card_id=card.id, business_id=card.business_id,
            kind=ArtifactKind.OTHER,
            label=(attempt.summary or "(shipped)")[:120],
            location=(attempt.detail or attempt.summary or "")[:240],
            is_primary=is_primary,
        )
        return

    url = matches[0].rstrip(".,;:")
    kind = ArtifactKind.URL
    lower = url.lower()
    if "/pull/" in lower or "/commit/" in lower:
        kind = ArtifactKind.PR
    elif any(host in lower for host in (
        ".vercel.app", ".pages.dev", ".netlify.app",
        "/deployments/",
    )):
        kind = ArtifactKind.DEPLOY
    svc.add(
        card_id=card.id, business_id=card.business_id,
        kind=kind,
        label=(attempt.summary or url)[:120],
        location=url,
        is_primary=is_primary,
    )


_ROLE_TAG_RE = re.compile(r"\[(CTO|CMO|COO|CEO|chief_of_staff|worker)\]", re.IGNORECASE)
_WORKER_TAG_RE = re.compile(
    r"\[WORKER:([a-z][a-z0-9_-]{0,40})\]", re.IGNORECASE,
)


def _worker_specialty_from_tag(task: str) -> str | None:
    """Extract the specialty from ``[WORKER:copywriter] write …``.

    Returns the lowercase specialty string, or None when the task
    doesn't carry a worker tag. Tolerates whitespace around the
    tag — anchor is the leading ``[`` after .lstrip()."""
    text = task.lstrip()
    if not text.startswith("["):
        return None
    match = _WORKER_TAG_RE.match(text)
    if match is None:
        return None
    return match.group(1).strip().lower()
_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _word_re(keyword: str) -> re.Pattern[str]:
    cached = _WORD_RE_CACHE.get(keyword)
    if cached is not None:
        return cached
    pattern = re.compile(rf"\b{re.escape(keyword)}\b")
    _WORD_RE_CACHE[keyword] = pattern
    return pattern


def _normalize(result: AttemptResult | BaseException, task: str) -> AttemptResult:
    if isinstance(result, AttemptResult):
        return result
    # An exception during attempt — report as an "error" status so the
    # caller can surface it without crashing the whole dispatch.
    return AttemptResult(
        role_type=RoleType.WORKER,
        title="(error)",
        status="error",
        summary=f"director failed on task: {task[:80]}",
        detail=f"{type(result).__name__}: {result}",
        blocker_ids=[],
        raw_response="",
        reasoning=None,
        cost_usd=0.0,
    )


def _cancelled_result(task: str) -> AttemptResult:
    """The founder interrupted this director mid-attempt. Surface as
    a blocked result so the CEO summarizer doesn't panic."""
    return AttemptResult(
        role_type=RoleType.WORKER,
        title="(interrupted)",
        status="blocked",
        summary=f"interrupted by founder: {task[:80]}",
        detail="The founder cancelled this sub-agent before it finished.",
        blocker_ids=[],
        raw_response="",
        reasoning=None,
        cost_usd=0.0,
    )


@dataclass
class DirectorFactory:
    """Encapsulates the wiring needed to build a Director.

    The CEO/CLI/tests construct one factory and pass it to
    Workforce.with_default_directors() to get a fully-wired team.
    """

    session: object  # SQLModel Session — typed loosely to avoid circular import
    cost_tracker: object  # CostTracker
    queue: object  # BlockerQueue
    hiring: object  # HiringService

    def build(self, *, personality: DirectorPersonality) -> Director:
        from sqlmodel import Session

        from korpha.blockers.queue import BlockerQueue
        from korpha.cofounder.hiring import HiringService
        from korpha.inference.cost_tracker import CostTracker

        assert isinstance(self.session, Session)
        assert isinstance(self.cost_tracker, CostTracker)
        assert isinstance(self.queue, BlockerQueue)
        assert isinstance(self.hiring, HiringService)

        return Director(
            personality=personality,
            session=self.session,
            cost_tracker=self.cost_tracker,
            queue=self.queue,
            hiring=self.hiring,
        )


@dataclass(frozen=True)
class DispatchSummary:
    """Aggregate view of one Workforce.dispatch() — what shipped, what
    blocked, total spend."""

    results: list[AttemptResult]
    shipped: int = field(default=0)
    blocked: int = field(default=0)
    errored: int = field(default=0)
    total_blockers: int = field(default=0)
    total_cost_usd: float = field(default=0.0)

    @classmethod
    def from_results(cls, results: list[AttemptResult]) -> DispatchSummary:
        shipped = sum(1 for r in results if r.status == "shipped")
        blocked = sum(1 for r in results if r.status == "blocked")
        errored = sum(1 for r in results if r.status == "error")
        total_blockers = sum(len(r.blocker_ids) for r in results)
        total_cost = sum(r.cost_usd for r in results)
        return cls(
            results=results,
            shipped=shipped,
            blocked=blocked,
            errored=errored,
            total_blockers=total_blockers,
            total_cost_usd=total_cost,
        )

    def headline(self) -> str:
        bits: list[str] = []
        if self.shipped:
            bits.append(f"{self.shipped} shipped")
        if self.blocked:
            bits.append(f"{self.blocked} blocked ({self.total_blockers} blockers)")
        if self.errored:
            bits.append(f"{self.errored} errored")
        return " • ".join(bits) or "no tasks"


def log_dispatch(
    *,
    session: object,
    business_id: UUID,
    summary: DispatchSummary,
) -> None:
    from sqlmodel import Session

    assert isinstance(session, Session)
    session.add(
        Activity(
            business_id=business_id,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            event_type="workforce.dispatch_completed",
            payload={
                "shipped": summary.shipped,
                "blocked": summary.blocked,
                "errored": summary.errored,
                "total_blockers": summary.total_blockers,
                "total_cost_usd": summary.total_cost_usd,
            },
        )
    )
    session.commit()
