"""Tests for the Workforce → Director/Worker business_unit_id plumb.

Verifies that when a kanban card has business_unit_id set but the unit
has no VP (so we fall back to a regular Director), the Director still
receives the unit context — so per-line BUDGET caps fire on costs from
that work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.business_units.model import BusinessUnit, BusinessUnitKind
from korpha.cofounder.workforce import Workforce
from korpha.identity.model import Founder
from korpha.kanban.model import KanbanCard, KanbanColumn
import korpha.db.registry  # noqa: F401


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_workforce_with_session(session: Session) -> Workforce:
    """Workforce with a fake director that exposes the right session."""
    director = MagicMock()
    director.session = session
    director.cost_tracker = MagicMock()
    director.personality = MagicMock()
    director.personality.role_type = MagicMock(value="cto")
    director.personality.domains = []
    wf = Workforce(directors={director.personality.role_type: director},
                   fallback_role=director.personality.role_type)
    return wf


def test_lookup_card_unit_id_finds_unit(session: Session) -> None:
    """When a backlog card matches the task title + has business_unit_id,
    _lookup_card_unit_id returns it."""
    founder = Founder(name="Mike", email="m@x.com")
    session.add(founder); session.commit(); session.refresh(founder)
    biz = Business(name="Marketro", founder_id=founder.id)
    session.add(biz); session.commit(); session.refresh(biz)
    unit = BusinessUnit(
        business_id=biz.id, name="POD", slug="pod",
        kind=BusinessUnitKind.LINE, memory_namespace_id=uuid4(),
    )
    session.add(unit); session.commit(); session.refresh(unit)
    card = KanbanCard(
        business_id=biz.id, business_unit_id=unit.id,
        title="Design hero shirt", column=KanbanColumn.BACKLOG,
        created_at=datetime.now(timezone.utc),
    )
    session.add(card); session.commit()

    wf = _make_workforce_with_session(session)
    result = wf._lookup_card_unit_id(
        "Design hero shirt", business_id=biz.id, session=session,
    )
    assert result == unit.id


def test_lookup_card_unit_id_returns_none_when_no_card(session: Session) -> None:
    founder = Founder(name="M", email="m@x.com")
    session.add(founder); session.commit(); session.refresh(founder)
    biz = Business(name="x", founder_id=founder.id)
    session.add(biz); session.commit(); session.refresh(biz)

    wf = _make_workforce_with_session(session)
    result = wf._lookup_card_unit_id(
        "task that has no card", business_id=biz.id, session=session,
    )
    assert result is None


def test_lookup_card_unit_id_returns_none_when_card_unscoped(session: Session) -> None:
    """A card without business_unit_id should yield None — costs
    legitimately count against company-wide caps only."""
    founder = Founder(name="M", email="m@x.com")
    session.add(founder); session.commit(); session.refresh(founder)
    biz = Business(name="x", founder_id=founder.id)
    session.add(biz); session.commit(); session.refresh(biz)
    card = KanbanCard(
        business_id=biz.id, business_unit_id=None,
        title="company-wide task", column=KanbanColumn.BACKLOG,
        created_at=datetime.now(timezone.utc),
    )
    session.add(card); session.commit()

    wf = _make_workforce_with_session(session)
    result = wf._lookup_card_unit_id(
        "company-wide task", business_id=biz.id, session=session,
    )
    assert result is None


def test_lookup_card_unit_id_strips_role_tag(session: Session) -> None:
    """When a task starts with [CTO] / [CMO] prefix, the title match
    should still work."""
    founder = Founder(name="M", email="m@x.com")
    session.add(founder); session.commit(); session.refresh(founder)
    biz = Business(name="x", founder_id=founder.id)
    session.add(biz); session.commit(); session.refresh(biz)
    unit = BusinessUnit(
        business_id=biz.id, name="POD", slug="pod",
        kind=BusinessUnitKind.LINE, memory_namespace_id=uuid4(),
    )
    session.add(unit); session.commit(); session.refresh(unit)
    card = KanbanCard(
        business_id=biz.id, business_unit_id=unit.id,
        title="ship it",
        column=KanbanColumn.BACKLOG,
        created_at=datetime.now(timezone.utc),
    )
    session.add(card); session.commit()

    wf = _make_workforce_with_session(session)
    result = wf._lookup_card_unit_id(
        "[CTO] ship it", business_id=biz.id, session=session,
    )
    assert result == unit.id
