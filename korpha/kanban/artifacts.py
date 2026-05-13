"""Typed artifacts (work products) attached to kanban cards.

Before this module, ``KanbanCard.review_evidence`` was a single
free-text field — "I posted to LinkedIn here: https://…". Worked,
but Mike couldn't tell at a glance whether it was a URL to click,
a file to read, or just hand-wavy prose.

Artifacts make the structure explicit: each piece of evidence
gets a kind (URL / FILE / DEPLOY / PR / MESSAGE / SCREENSHOT),
its own ``review_state`` (pending / accepted / rework), and an
optional health probe URL for deploys. The dashboard renders
each as a tappable link with an accept/kickback button.

Backward compatible: legacy review_evidence still renders for
old cards. New cards use artifacts; we never delete the prose
field. Workforce.dispatch() emits one artifact per shipped
attempt (kind=URL with the result summary) — same evidence,
new shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional
from uuid import UUID

from sqlmodel import Field, Session, SQLModel, select

from korpha.db._base import primary_key_field, timestamp_field


class ArtifactKind(StrEnum):
    """What flavor of evidence is this?"""

    URL = "url"
    """Generic web URL — landing page, social post, blog draft."""

    DEPLOY = "deploy"
    """Live deployed surface (with optional health-probe URL)."""

    PR = "pr"
    """A pull request / commit link. Pairs with the workspaces
    + checkpoints v2 layer."""

    FILE = "file"
    """Filesystem path inside the Korpha data dir or a
    workspace. Skill-authored copy, image output, etc."""

    MESSAGE = "message"
    """A sent message id — outreach DM, support reply,
    notification confirmation."""

    SCREENSHOT = "screenshot"
    """An image attachment (rendered inline on the dashboard)."""

    DOC = "doc"
    """Long-form document — research note, policy draft."""

    OTHER = "other"


class ArtifactReviewState(StrEnum):
    """Per-artifact verdict so a card can have some accepted
    artifacts and others kicked back."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REWORK = "rework"


class CardArtifact(SQLModel, table=True):
    """One typed artifact tied to a kanban card."""

    __tablename__ = "kanban_card_artifact"

    id: UUID = primary_key_field()
    card_id: UUID = Field(foreign_key="kanban_card.id", index=True)
    business_id: UUID = Field(foreign_key="business.id", index=True)

    kind: ArtifactKind = Field(default=ArtifactKind.URL, index=True)
    label: str = Field(
        description=(
            "Short human-readable label. Shown as the link text "
            "in /app/kanban. e.g. 'pricing page' / 'PR #42' / "
            "'support reply to ticket 81'."
        ),
    )
    location: str = Field(
        description=(
            "The URL, file path, message id, or commit hash — "
            "whatever uniquely identifies the artifact."
        ),
    )

    health_url: Optional[str] = Field(
        default=None,
        description=(
            "When kind=deploy, optional /healthz or root URL we "
            "can probe to confirm the artifact is still live. "
            "Periodic check is future work; today this just shows "
            "in the UI as a 'check status' link."
        ),
    )

    review_state: ArtifactReviewState = Field(
        default=ArtifactReviewState.PENDING, index=True,
    )
    reviewed_at: Optional[datetime] = Field(default=None)
    reviewer_note: Optional[str] = Field(
        default=None,
        description="Why the founder accepted/rejected this artifact.",
    )

    is_primary: bool = Field(
        default=False,
        description=(
            "Mark one artifact per card as primary — the headline "
            "deliverable. Renders larger on the kanban card; "
            "weekly digest references the primary's location as "
            "the card's deliverable URL."
        ),
    )

    created_at: datetime = timestamp_field()


@dataclass
class ArtifactService:
    """Per-Session artifact ops."""

    session: Session

    def add(
        self,
        *,
        card_id: UUID,
        business_id: UUID,
        kind: ArtifactKind,
        label: str,
        location: str,
        health_url: Optional[str] = None,
        is_primary: bool = False,
    ) -> CardArtifact:
        if not label.strip():
            raise ValueError("artifact: label required")
        if not location.strip():
            raise ValueError("artifact: location required")

        # Only one primary per card — clear any existing primary
        # before flagging this one.
        if is_primary:
            for existing in self.list_for_card(card_id):
                if existing.is_primary:
                    existing.is_primary = False
                    self.session.add(existing)
            self.session.commit()

        art = CardArtifact(
            card_id=card_id,
            business_id=business_id,
            kind=kind,
            label=label.strip(),
            location=location.strip(),
            health_url=health_url,
            is_primary=is_primary,
        )
        self.session.add(art)
        self.session.commit()
        self.session.refresh(art)
        return art

    def list_for_card(self, card_id: UUID) -> list[CardArtifact]:
        return list(self.session.exec(
            select(CardArtifact)
            .where(CardArtifact.card_id == card_id)
            .order_by(CardArtifact.created_at)  # type: ignore[arg-type]
        ).all())

    def review(
        self,
        artifact_id: UUID,
        *,
        state: ArtifactReviewState,
        note: Optional[str] = None,
    ) -> CardArtifact:
        art = self.session.get(CardArtifact, artifact_id)
        if art is None:
            raise KeyError(f"artifact {artifact_id} not found")
        if state == ArtifactReviewState.PENDING:
            raise ValueError(
                "artifact: cannot review back to PENDING; use "
                "delete or leave unreviewed"
            )
        from datetime import datetime as _dt, timezone as _tz
        art.review_state = state
        art.reviewer_note = note
        art.reviewed_at = _dt.now(tz=_tz.utc)
        self.session.add(art)
        self.session.commit()
        self.session.refresh(art)
        return art

    def delete(self, artifact_id: UUID) -> bool:
        art = self.session.get(CardArtifact, artifact_id)
        if art is None:
            return False
        self.session.delete(art)
        self.session.commit()
        return True


__all__ = [
    "ArtifactKind",
    "ArtifactReviewState",
    "ArtifactService",
    "CardArtifact",
]
