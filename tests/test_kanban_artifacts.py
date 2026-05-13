"""Tests for kanban card artifacts (typed work products)."""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.kanban import (
    ArtifactKind, ArtifactReviewState, ArtifactService,
    CardArtifact, CreateCardInput, KanbanBoard,
)
from korpha.kanban.model import KanbanColumn


def _make_card(session, business):
    board = KanbanBoard(session)
    return board.create(CreateCardInput(
        business_id=business.id, title="x",
    ))


# ---- ArtifactService.add ----


def test_add_persists_artifact(
    session: Session, business: Business,
) -> None:
    card = _make_card(session, business)
    svc = ArtifactService(session)
    art = svc.add(
        card_id=card.id, business_id=business.id,
        kind=ArtifactKind.URL,
        label="pricing page",
        location="https://example.com/pricing",
    )
    assert art.id is not None
    assert art.review_state == ArtifactReviewState.PENDING
    assert art.is_primary is False


def test_add_blank_label_rejected(
    session: Session, business: Business,
) -> None:
    card = _make_card(session, business)
    svc = ArtifactService(session)
    with pytest.raises(ValueError, match="label"):
        svc.add(
            card_id=card.id, business_id=business.id,
            kind=ArtifactKind.URL, label="  ",
            location="https://x.com",
        )


def test_add_blank_location_rejected(
    session: Session, business: Business,
) -> None:
    card = _make_card(session, business)
    svc = ArtifactService(session)
    with pytest.raises(ValueError, match="location"):
        svc.add(
            card_id=card.id, business_id=business.id,
            kind=ArtifactKind.URL, label="x", location="",
        )


def test_add_primary_clears_existing_primary(
    session: Session, business: Business,
) -> None:
    """Only one primary per card."""
    card = _make_card(session, business)
    svc = ArtifactService(session)
    svc.add(
        card_id=card.id, business_id=business.id,
        kind=ArtifactKind.URL, label="first",
        location="https://1.com", is_primary=True,
    )
    svc.add(
        card_id=card.id, business_id=business.id,
        kind=ArtifactKind.URL, label="second",
        location="https://2.com", is_primary=True,
    )
    arts = svc.list_for_card(card.id)
    primaries = [a for a in arts if a.is_primary]
    assert len(primaries) == 1
    assert primaries[0].label == "second"


# ---- review ----


def test_review_marks_accepted(
    session: Session, business: Business,
) -> None:
    card = _make_card(session, business)
    svc = ArtifactService(session)
    art = svc.add(
        card_id=card.id, business_id=business.id,
        kind=ArtifactKind.URL, label="x",
        location="https://x.com",
    )
    reviewed = svc.review(
        art.id, state=ArtifactReviewState.ACCEPTED,
        note="looks great",
    )
    assert reviewed.review_state == ArtifactReviewState.ACCEPTED
    assert reviewed.reviewer_note == "looks great"
    assert reviewed.reviewed_at is not None


def test_review_back_to_pending_rejected(
    session: Session, business: Business,
) -> None:
    card = _make_card(session, business)
    svc = ArtifactService(session)
    art = svc.add(
        card_id=card.id, business_id=business.id,
        kind=ArtifactKind.URL, label="x",
        location="https://x.com",
    )
    with pytest.raises(ValueError, match="PENDING"):
        svc.review(art.id, state=ArtifactReviewState.PENDING)


def test_review_unknown_id(session: Session) -> None:
    from uuid import uuid4
    svc = ArtifactService(session)
    with pytest.raises(KeyError, match="not found"):
        svc.review(uuid4(), state=ArtifactReviewState.ACCEPTED)


# ---- delete ----


def test_delete_removes(session: Session, business: Business) -> None:
    card = _make_card(session, business)
    svc = ArtifactService(session)
    art = svc.add(
        card_id=card.id, business_id=business.id,
        kind=ArtifactKind.URL, label="x", location="https://x",
    )
    assert svc.delete(art.id) is True
    assert svc.list_for_card(card.id) == []


def test_delete_unknown_returns_false(session: Session) -> None:
    from uuid import uuid4
    assert ArtifactService(session).delete(uuid4()) is False


# ---- workforce auto-emit ----


def _hire_cto(session, business):
    role = AgentRole(
        business_id=business.id, role_type=RoleType.CTO, title="CTO",
    )
    session.add(role); session.commit(); session.refresh(role)
    return role


@pytest.mark.asyncio
async def test_shipped_attempt_emits_url_artifact(
    session: Session, business: Business, founder: Founder,
) -> None:
    """End-to-end: workforce.dispatch on a shipped attempt with
    a URL in summary creates an ArtifactKind.URL on the card."""
    from korpha.cofounder.director import (
        AttemptResult, DEFAULT_PERSONALITIES,
    )
    from korpha.cofounder.workforce import Workforce

    cto = _hire_cto(session, business)
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="ship landing",
    ))

    class _StubDirector:
        personality = DEFAULT_PERSONALITIES[RoleType.CTO]

        def __init__(self, session):
            self.session = session

        async def attempt(self, *, business, founder, task):
            return AttemptResult(
                role_type=RoleType.CTO, title=task,
                status="shipped",
                summary=(
                    "Deployed the page at "
                    "https://example.com/pricing"
                ),
                detail="merged PR #42",
                blocker_ids=[], raw_response="",
                reasoning=None, cost_usd=0.0,
            )

    workforce = Workforce(
        directors={RoleType.CTO: _StubDirector(session)},  # type: ignore[arg-type]
    )
    await workforce.dispatch(
        business=business, founder=founder,
        tasks=["[CTO] ship landing"],
    )

    arts = list(session.exec(select(CardArtifact)).all())
    assert len(arts) == 1
    assert arts[0].kind == ArtifactKind.URL
    assert "https://example.com/pricing" in arts[0].location
    assert arts[0].is_primary is True


@pytest.mark.asyncio
async def test_shipped_attempt_classifies_pr_url(
    session: Session, business: Business, founder: Founder,
) -> None:
    from korpha.cofounder.director import (
        AttemptResult, DEFAULT_PERSONALITIES,
    )
    from korpha.cofounder.workforce import Workforce

    cto = _hire_cto(session, business)
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="merge fix",
    ))

    class _StubDirector:
        personality = DEFAULT_PERSONALITIES[RoleType.CTO]

        def __init__(self, session):
            self.session = session

        async def attempt(self, *, business, founder, task):
            return AttemptResult(
                role_type=RoleType.CTO, title=task,
                status="shipped",
                summary=(
                    "Opened https://github.com/x/y/pull/42 with "
                    "the fix"
                ),
                detail=None,
                blocker_ids=[], raw_response="",
                reasoning=None, cost_usd=0.0,
            )

    workforce = Workforce(
        directors={RoleType.CTO: _StubDirector(session)},  # type: ignore[arg-type]
    )
    await workforce.dispatch(
        business=business, founder=founder,
        tasks=["[CTO] merge fix"],
    )
    art = session.exec(select(CardArtifact)).one()
    assert art.kind == ArtifactKind.PR


@pytest.mark.asyncio
async def test_shipped_attempt_classifies_deploy_url(
    session: Session, business: Business, founder: Founder,
) -> None:
    from korpha.cofounder.director import (
        AttemptResult, DEFAULT_PERSONALITIES,
    )
    from korpha.cofounder.workforce import Workforce

    cto = _hire_cto(session, business)
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="ship vercel",
    ))

    class _StubDirector:
        personality = DEFAULT_PERSONALITIES[RoleType.CTO]

        def __init__(self, session):
            self.session = session

        async def attempt(self, *, business, founder, task):
            return AttemptResult(
                role_type=RoleType.CTO, title=task,
                status="shipped",
                summary="https://my-project.vercel.app",
                detail=None,
                blocker_ids=[], raw_response="",
                reasoning=None, cost_usd=0.0,
            )

    workforce = Workforce(
        directors={RoleType.CTO: _StubDirector(session)},  # type: ignore[arg-type]
    )
    await workforce.dispatch(
        business=business, founder=founder,
        tasks=["[CTO] ship vercel"],
    )
    art = session.exec(select(CardArtifact)).one()
    assert art.kind == ArtifactKind.DEPLOY


@pytest.mark.asyncio
async def test_shipped_attempt_no_url_creates_other_artifact(
    session: Session, business: Business, founder: Founder,
) -> None:
    """When the agent ships without producing a URL, we still
    emit an OTHER artifact so /app/kanban has *something* to
    render — better than empty."""
    from korpha.cofounder.director import (
        AttemptResult, DEFAULT_PERSONALITIES,
    )
    from korpha.cofounder.workforce import Workforce

    cto = _hire_cto(session, business)
    board = KanbanBoard(session)
    board.create(CreateCardInput(
        business_id=business.id, title="thinking task",
    ))

    class _StubDirector:
        personality = DEFAULT_PERSONALITIES[RoleType.CTO]

        def __init__(self, session):
            self.session = session

        async def attempt(self, *, business, founder, task):
            return AttemptResult(
                role_type=RoleType.CTO, title=task,
                status="shipped",
                summary="Reviewed the architecture options",
                detail="long deliberation, no concrete artifact",
                blocker_ids=[], raw_response="",
                reasoning=None, cost_usd=0.0,
            )

    workforce = Workforce(
        directors={RoleType.CTO: _StubDirector(session)},  # type: ignore[arg-type]
    )
    await workforce.dispatch(
        business=business, founder=founder,
        tasks=["[CTO] thinking task"],
    )
    art = session.exec(select(CardArtifact)).one()
    assert art.kind == ArtifactKind.OTHER
    assert "Reviewed" in art.label
