"""GoalManager — sets / pauses / resumes / clears + evaluates after each turn."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session as _SAOrmSession
from sqlmodel import Session, select

from korpha.audit.model import InferenceTier
from korpha.db._base import utcnow
from korpha.goals.judge import (
    DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES,
    DEFAULT_MAX_TURNS,
    JUDGE_SYSTEM_PROMPT,
    JUDGE_USER_PROMPT_TEMPLATE,
    JudgeVerdict,
    parse_judge_response,
    truncate_response,
)
from korpha.goals.model import Goal, GoalStatus

logger = logging.getLogger(__name__)


CONTINUATION_PROMPT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Continue working toward this goal. Take the next concrete step. "
    "If you believe the goal is complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly "
    "and stop."
)


def continuation_prompt_for(goal_text: str) -> str:
    """The user-message body fed back into the agent on each
    continuation turn. Stable string so prompt caching can reuse
    the goal-context prefix across turns."""
    return CONTINUATION_PROMPT_TEMPLATE.format(goal=goal_text)


class GoalReplaceConflict(RuntimeError):
    """Raised by ``GoalManager.set()`` when called against a thread
    that already has an ACTIVE goal and ``force=False``. Prevents
    silent replacement that could race two judges against the
    same thread."""


class GoalManager:
    """Per-thread goal API + judge-driven loop helpers.

    A GoalManager wraps a session + thread_id; it does NOT run
    the agent loop itself. The caller (CLI / TUI / handler)
    composes ``manager.set(...)`` → ``ceo.handle(...)`` →
    ``manager.evaluate_after_turn(reply_text)`` → either commit a
    continuation or stop.

    Cardinality: at most one ACTIVE goal per (thread_id). New
    set() while one is active replaces it (the old goal moves
    to CLEARED).
    """

    def __init__(
        self,
        session: Session,
        *,
        thread_id: UUID,
        business_id: UUID,
        cost_tracker: Any,
        judge_tier: InferenceTier = InferenceTier.WORKHORSE,
    ) -> None:
        self.session = session
        self.thread_id = thread_id
        self.business_id = business_id
        self.cost_tracker = cost_tracker
        self.judge_tier = judge_tier

    # ---- read ----

    def active(self) -> Goal | None:
        """The current ACTIVE goal on this thread, if any."""
        return self.session.exec(
            select(Goal)
            .where(Goal.thread_id == self.thread_id)
            .where(Goal.status == GoalStatus.ACTIVE)
        ).first()

    def latest(self) -> Goal | None:
        """Most-recently-created goal regardless of status. Useful
        for resume() and for the "what was the last goal?" question
        in the dashboard."""
        return self.session.exec(
            select(Goal)
            .where(Goal.thread_id == self.thread_id)
            .order_by(Goal.created_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()

    def is_active(self) -> bool:
        return self.active() is not None

    # ---- write ----

    def set(
        self,
        goal_text: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        force: bool = False,
    ) -> Goal:
        """Set or replace the active goal on this thread.

        Refuses to replace an existing ACTIVE goal unless ``force=True``
        — avoids racing two judges against the same thread when the
        continuation loop is mid-flight. Caller (CLI / slash handler)
        surfaces ``GoalReplaceConflict`` as a friendly "clear first,
        or pass --force" message.

        Paused / cleared / achieved goals never trip the guard —
        only ACTIVE means "judge could be running right now".
        """
        goal_text = (goal_text or "").strip()
        if not goal_text:
            raise ValueError("goal text cannot be empty")
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")

        existing = self.active()
        if existing is not None:
            if not force:
                raise GoalReplaceConflict(
                    f"An active goal exists ({existing.turns_used}/"
                    f"{existing.max_turns} turns): {existing.text}. "
                    "Run `/goal clear` first, or pass --force to "
                    "replace it mid-run."
                )
            existing.status = GoalStatus.CLEARED
            existing.paused_reason = "replaced-by-new-goal"
            existing.finished_at = utcnow()
            existing.updated_at = utcnow()
            self.session.add(existing)

        goal = Goal(
            id=uuid4(),
            thread_id=self.thread_id,
            business_id=self.business_id,
            text=goal_text,
            max_turns=max_turns,
            status=GoalStatus.ACTIVE,
        )
        self.session.add(goal)
        self.session.commit()
        self.session.refresh(goal)
        return goal

    def pause(self, *, reason: str = "user-paused") -> Goal | None:
        """Move the active goal to PAUSED. The continuation loop
        stops; founder calls resume() to restart."""
        goal = self.active()
        if goal is None:
            return None
        goal.status = GoalStatus.PAUSED
        goal.paused_reason = reason
        goal.updated_at = utcnow()
        self.session.add(goal)
        self.session.commit()
        self.session.refresh(goal)
        return goal

    def resume(self, *, reset_budget: bool = True) -> Goal | None:
        """Move the most-recent paused goal back to ACTIVE. By
        default also resets the turn budget — founder explicitly
        opted in for "keep going" so don't trip them on the budget
        immediately."""
        latest = self.session.exec(
            select(Goal)
            .where(Goal.thread_id == self.thread_id)
            .where(Goal.status == GoalStatus.PAUSED)
            .order_by(Goal.created_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()
        if latest is None:
            return None
        latest.status = GoalStatus.ACTIVE
        latest.paused_reason = None
        if reset_budget:
            latest.turns_used = 0
            latest.consecutive_parse_failures = 0
        latest.updated_at = utcnow()
        self.session.add(latest)
        self.session.commit()
        self.session.refresh(latest)
        return latest

    def clear(self) -> Goal | None:
        """Drop the active goal entirely. Goal moves to CLEARED
        (kept for audit) so /goal status doesn't show it but
        history does."""
        goal = self.active()
        if goal is None:
            return None
        goal.status = GoalStatus.CLEARED
        goal.finished_at = utcnow()
        goal.updated_at = utcnow()
        self.session.add(goal)
        self.session.commit()
        self.session.refresh(goal)
        return goal

    def mark_done(self, reason: str) -> Goal | None:
        """Force-complete the active goal — the judge can also do
        this via evaluate_after_turn but founders can override."""
        goal = self.active()
        if goal is None:
            return None
        goal.status = GoalStatus.DONE
        goal.last_verdict = "done"
        goal.last_reason = reason
        goal.finished_at = utcnow()
        goal.updated_at = utcnow()
        self.session.add(goal)
        self.session.commit()
        self.session.refresh(goal)
        return goal

    # ---- the loop ----

    async def evaluate_after_turn(
        self, *, last_response: str,
    ) -> Goal | None:
        """Run the judge against ``last_response``. Updates the
        active goal's verdict + status. Returns the (refreshed)
        Goal, or None if no goal is active.

        Status transitions:
          - judge says done → DONE
          - judge says continue + budget remaining → ACTIVE
            (turns_used++)
          - judge says continue + budget exhausted → PAUSED
            (paused_reason='turn-budget')
          - 3 consecutive parse failures → PAUSED
            (paused_reason='judge-parse-failures')
        """
        goal = self.active()
        if goal is None:
            return None

        verdict = await self._call_judge(goal.text, last_response)
        goal.turns_used += 1
        goal.last_verdict = "done" if verdict.done else "continue"
        goal.last_reason = verdict.reason
        goal.updated_at = utcnow()

        if not verdict.parsed:
            goal.consecutive_parse_failures += 1
            if (
                goal.consecutive_parse_failures
                >= DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES
            ):
                goal.status = GoalStatus.PAUSED
                goal.paused_reason = "judge-parse-failures"
        else:
            goal.consecutive_parse_failures = 0
            if verdict.done:
                goal.status = GoalStatus.DONE
                goal.finished_at = utcnow()
            elif goal.turns_used >= goal.max_turns:
                goal.status = GoalStatus.PAUSED
                goal.paused_reason = "turn-budget"

        self.session.add(goal)
        self.session.commit()
        self.session.refresh(goal)
        return goal

    def next_continuation_prompt(self) -> str | None:
        """The user-message body to feed back for the next loop
        iteration. None when no goal is active (caller stops).

        From turn 2 onward, prefixes the continuation with a
        bounded summary of prior turns (verdict + reason +
        paused state) so the worker doesn't re-derive context
        from raw transcript history."""
        goal = self.active()
        if goal is None:
            return None
        base = continuation_prompt_for(goal.text)
        try:
            from korpha.continuation import summarize_goal_history

            summary = summarize_goal_history(self.session, goal.id)
        except Exception:  # noqa: BLE001
            summary = None
        if summary is None:
            return base
        return f"{summary.text}\n\n---\n\n{base}"

    # ---- internals ----

    async def _call_judge(
        self, goal_text: str, last_response: str,
    ) -> JudgeVerdict:
        """Run the judge LLM call. Network errors → fail-OPEN
        (treat as continue) so a transient outage doesn't wedge
        the loop. The turn-budget backstop catches runaway
        continuations."""
        from korpha.inference import (
            CompletionRequest, Message as LlmMessage, Role,
        )

        prompt = JUDGE_USER_PROMPT_TEMPLATE.format(
            goal=goal_text,
            response=truncate_response(last_response),
        )
        request = CompletionRequest(
            messages=[
                LlmMessage(role=Role.SYSTEM, content=JUDGE_SYSTEM_PROMPT),
                LlmMessage(role=Role.USER, content=prompt),
            ],
            tier=self.judge_tier,
            session_key=f"goal-judge-{self.thread_id}",
            max_tokens=400,
            timeout_seconds=30.0,
        )
        try:
            response = await self.cost_tracker.complete(
                request,
                session=self.session,
                business_id=self.business_id,
                thread_id=self.thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Transport / API failure — fail OPEN: treat as
            # continue (don't wedge). Doesn't count toward parse-
            # failure budget.
            logger.warning(
                "goal judge transport failure: %s; treating as continue", exc,
            )
            return JudgeVerdict(
                done=False,
                reason=f"(judge call failed: {exc})",
                parsed=True,  # not a parse failure — transport fault
            )
        return parse_judge_response(response.content or "")


__all__ = ["GoalManager", "continuation_prompt_for"]
