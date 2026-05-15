"""BlockerQueue — agents submit blockers, the queue persists and dedupes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from sqlmodel import Session, select

from korpha.audit.model import Activity, ActorType
from korpha.blockers.model import (
    Blocker,
    BlockerKind,
    BlockerStatus,
    BlockerUrgency,
)
from korpha.db._base import as_utc, utcnow

DEDUPE_WINDOW = timedelta(hours=24)


@dataclass
class BlockerSubmission:
    """Input to BlockerQueue.submit() — collected from any agent."""

    business_id: UUID
    requesting_agent_role_id: UUID
    title: str
    kind: BlockerKind = BlockerKind.OTHER
    urgency: BlockerUrgency = BlockerUrgency.NORMAL
    detail: str = ""
    options: list[str] = field(default_factory=list)
    task_id: UUID | None = None
    kanban_card_id: UUID | None = None
    parent_blocker_id: UUID | None = None
    topic_tag: str | None = None


@dataclass
class BlockerQueue:
    """SQLModel-backed queue. Stateless apart from the session."""

    session: Session

    def submit(self, submission: BlockerSubmission) -> Blocker:
        """Persist a blocker, dedupe against recent open ones with the same title."""
        existing = self._find_recent_dupe(submission)
        if existing is not None:
            dupe = self._record_duplicate(existing, submission)
            self._log(submission.business_id, dupe.id, "blocker.duplicate")
            return dupe

        blocker = Blocker(
            business_id=submission.business_id,
            requesting_agent_role_id=submission.requesting_agent_role_id,
            task_id=submission.task_id,
            kanban_card_id=submission.kanban_card_id,
            parent_blocker_id=submission.parent_blocker_id,
            kind=submission.kind,
            urgency=submission.urgency,
            title=submission.title.strip(),
            detail=submission.detail,
            options=list(submission.options),
            topic_tag=submission.topic_tag,
            status=BlockerStatus.OPEN,
        )
        self.session.add(blocker)
        self.session.commit()
        self.session.refresh(blocker)
        self._log(blocker.business_id, blocker.id, "blocker.submitted")
        return blocker

    def get(self, blocker_id: UUID) -> Blocker:
        blocker = self.session.get(Blocker, blocker_id)
        if blocker is None:
            raise KeyError(f"Blocker {blocker_id} not found")
        return blocker

    def list_open(
        self,
        business_id: UUID,
        *,
        statuses: tuple[BlockerStatus, ...] = (
            BlockerStatus.OPEN,
            BlockerStatus.TRIAGED,
            BlockerStatus.AWAITING_FOUNDER,
        ),
    ) -> list[Blocker]:
        stmt = (
            select(Blocker)
            .where(Blocker.business_id == business_id)
            .where(Blocker.deduped_into_id.is_(None))  # type: ignore[union-attr]
        )
        rows = list(self.session.exec(stmt).all())
        return [b for b in rows if b.status in statuses]

    def update(self, blocker: Blocker) -> Blocker:
        self.session.add(blocker)
        self.session.commit()
        self.session.refresh(blocker)
        return blocker

    def mark_resolved(
        self,
        blocker_id: UUID,
        *,
        resolution: str,
        resolved_by_founder_id: UUID | None = None,
        by_cos: bool = False,
    ) -> Blocker:
        blocker = self.get(blocker_id)
        now = utcnow()
        blocker.status = (
            BlockerStatus.RESOLVED_BY_COS if by_cos else BlockerStatus.RESOLVED
        )
        blocker.resolution = resolution
        blocker.resolved_at = now
        blocker.resolved_by_founder_id = resolved_by_founder_id
        self.session.add(blocker)
        self.session.commit()
        self.session.refresh(blocker)
        self._log(
            blocker.business_id,
            blocker.id,
            "blocker.resolved_by_cos" if by_cos else "blocker.resolved",
        )
        return blocker

    def _find_recent_dupe(self, submission: BlockerSubmission) -> Blocker | None:
        """Same-business + same-title (case-insensitive) within DEDUPE_WINDOW.

        We dedupe by title rather than detail to avoid a single agent's small
        wording variations producing duplicates.
        """
        cutoff = utcnow() - DEDUPE_WINDOW
        normalized = submission.title.strip().lower()
        stmt = (
            select(Blocker)
            .where(Blocker.business_id == submission.business_id)
            .where(Blocker.deduped_into_id.is_(None))  # type: ignore[union-attr]
        )
        for candidate in self.session.exec(stmt).all():
            if candidate.title.strip().lower() != normalized:
                continue
            if candidate.status in (
                BlockerStatus.RESOLVED,
                BlockerStatus.RESOLVED_BY_COS,
                BlockerStatus.DROPPED,
            ):
                continue
            submitted = as_utc(candidate.submitted_at)
            if submitted is None or submitted < cutoff:
                continue
            return candidate
        return None

    def _record_duplicate(
        self, canonical: Blocker, submission: BlockerSubmission
    ) -> Blocker:
        dupe = Blocker(
            business_id=submission.business_id,
            requesting_agent_role_id=submission.requesting_agent_role_id,
            task_id=submission.task_id,
            kind=submission.kind,
            urgency=submission.urgency,
            title=submission.title.strip(),
            detail=submission.detail,
            options=list(submission.options),
            topic_tag=submission.topic_tag,
            status=BlockerStatus.DROPPED,
            deduped_into_id=canonical.id,
        )
        self.session.add(dupe)
        # Bump canonical urgency if the new submission is higher.
        if _urgency_rank(submission.urgency) > _urgency_rank(canonical.urgency):
            canonical.urgency = submission.urgency
            self.session.add(canonical)
        self.session.commit()
        self.session.refresh(dupe)
        return dupe

    def _log(self, business_id: UUID, blocker_id: UUID, event: str) -> None:
        self.session.add(
            Activity(
                business_id=business_id,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                event_type=event,
                payload={"blocker_id": str(blocker_id)},
            )
        )
        self.session.commit()


def _urgency_rank(u: BlockerUrgency) -> int:
    return {
        BlockerUrgency.LOW: 0,
        BlockerUrgency.NORMAL: 1,
        BlockerUrgency.HIGH: 2,
        BlockerUrgency.URGENT: 3,
    }[u]
