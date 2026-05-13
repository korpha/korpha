"""PR9 tests — memory namespace_id + CrossNamespaceRecallGrant hook +
authorization helper.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.cooperation.board import CooperationBoard
from korpha.cooperation.model import CooperationStatus
from korpha.identity.model import Founder
from korpha.memory.grants import (
    CrossNamespaceRecallGrant,
    check_recall_authorized,
    issue_grant_from_proposal,
    revoke_grants_for_proposal,
)
from korpha.memory.model import LongTermMemoryEntry


@pytest.fixture
def two_units(
    session: Session, business: Business,
) -> tuple[BusinessUnit, BusinessUnit]:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    a = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    b = board.create(
        business_id=business.id, name="POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    return a, b


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_long_term_memory_has_namespace_column() -> None:
    cols = {c.name for c in LongTermMemoryEntry.__table__.columns}
    assert "namespace_id" in cols
    nullable = LongTermMemoryEntry.__table__.c["namespace_id"].nullable
    assert nullable is True


def test_grant_table_registered() -> None:
    cols = {c.name for c in CrossNamespaceRecallGrant.__table__.columns}
    assert {
        "from_namespace_id", "to_namespace_id",
        "cooperation_proposal_id", "is_active",
        "granted_at", "expires_at",
    } <= cols


# ---------------------------------------------------------------------------
# check_recall_authorized
# ---------------------------------------------------------------------------


def test_same_namespace_always_authorized(
    session: Session, two_units,
) -> None:
    a, _ = two_units
    assert check_recall_authorized(
        session,
        from_namespace_id=a.memory_namespace_id,
        to_namespace_id=a.memory_namespace_id,
    )


def test_cross_namespace_blocked_by_default(
    session: Session, two_units,
) -> None:
    a, b = two_units
    assert not check_recall_authorized(
        session,
        from_namespace_id=a.memory_namespace_id,
        to_namespace_id=b.memory_namespace_id,
    )


def test_active_grant_authorizes_cross_namespace(
    session: Session, business: Business, two_units,
) -> None:
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="ask about reader survey",
        permissions={"cross_namespace_recall": True},
    )
    coop_board.decide(
        prop.id, decision=CooperationStatus.ACCEPTED,
    )
    # Hook should have created a grant
    grants = list(session.exec(
        select(CrossNamespaceRecallGrant)
    ).all())
    assert len(grants) == 1
    assert check_recall_authorized(
        session,
        from_namespace_id=a.memory_namespace_id,
        to_namespace_id=b.memory_namespace_id,
    )


def test_revoked_grant_blocks_recall(
    session: Session, business: Business, two_units,
) -> None:
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="ask",
        permissions={"cross_namespace_recall": True},
    )
    coop_board.decide(prop.id, decision=CooperationStatus.ACCEPTED)
    assert check_recall_authorized(
        session,
        from_namespace_id=a.memory_namespace_id,
        to_namespace_id=b.memory_namespace_id,
    )
    coop_board.revoke(prop.id)
    assert not check_recall_authorized(
        session,
        from_namespace_id=a.memory_namespace_id,
        to_namespace_id=b.memory_namespace_id,
    )


def test_expired_grant_blocks_recall(
    session: Session, business: Business, two_units,
) -> None:
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="ask",
        permissions={"cross_namespace_recall": True},
        expires_at=datetime.now(UTC) - timedelta(days=1),  # already expired
    )
    coop_board.decide(prop.id, decision=CooperationStatus.ACCEPTED)
    assert not check_recall_authorized(
        session,
        from_namespace_id=a.memory_namespace_id,
        to_namespace_id=b.memory_namespace_id,
    )


def test_grant_without_expiration_authorizes_indefinitely(
    session: Session, business: Business, two_units,
) -> None:
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="ask",
        permissions={"cross_namespace_recall": True},
    )
    coop_board.decide(prop.id, decision=CooperationStatus.ACCEPTED)
    # check 5 years from now
    way_future = datetime(2031, 1, 1, tzinfo=UTC)
    assert check_recall_authorized(
        session,
        from_namespace_id=a.memory_namespace_id,
        to_namespace_id=b.memory_namespace_id,
        now=way_future,
    )


# ---------------------------------------------------------------------------
# Hook integration
# ---------------------------------------------------------------------------


def test_accept_without_recall_permission_no_grant(
    session: Session, business: Business, two_units,
) -> None:
    """ACCEPT on a proposal WITHOUT cross_namespace_recall=True →
    no grant row created."""
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="merch only",
        permissions={"royalty_share_pct": 20},  # no recall permission
    )
    coop_board.decide(prop.id, decision=CooperationStatus.ACCEPTED)
    grants = list(session.exec(
        select(CrossNamespaceRecallGrant)
    ).all())
    assert grants == []


def test_decline_does_not_create_grant(
    session: Session, business: Business, two_units,
) -> None:
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="x",
        permissions={"cross_namespace_recall": True},
    )
    coop_board.decide(prop.id, decision=CooperationStatus.DECLINED)
    grants = list(session.exec(
        select(CrossNamespaceRecallGrant)
    ).all())
    assert grants == []


def test_issue_grant_from_proposal_direct(
    session: Session, business: Business, two_units,
) -> None:
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="x",
    )
    grant = issue_grant_from_proposal(
        session,
        proposal_id=prop.id,
        from_unit_id=a.id, to_unit_id=b.id,
    )
    assert grant.from_namespace_id == a.memory_namespace_id
    assert grant.to_namespace_id == b.memory_namespace_id
    assert grant.is_active is True


def test_revoke_grants_for_proposal_flips_active(
    session: Session, business: Business, two_units,
) -> None:
    a, b = two_units
    coop_board = CooperationBoard(session)
    prop = coop_board.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="x",
    )
    grant = issue_grant_from_proposal(
        session, proposal_id=prop.id,
        from_unit_id=a.id, to_unit_id=b.id,
    )
    count = revoke_grants_for_proposal(session, prop.id)
    assert count == 1
    session.refresh(grant)
    assert grant.is_active is False
