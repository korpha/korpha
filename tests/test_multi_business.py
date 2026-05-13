"""Multi-business resolution: active business pointer, list, create, switch."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.business.multi import (
    BusinessResolutionError,
    active_business,
    create_business,
    list_businesses,
    switch_active,
)
from korpha.identity.model import Founder


def test_no_businesses_raises(session: Session, founder: Founder) -> None:
    with pytest.raises(BusinessResolutionError) as exc:
        active_business(session, founder)
    assert "no businesses" in str(exc.value).lower()


def test_single_business_is_implicitly_active(
    session: Session, founder: Founder, business: Business
) -> None:
    """The 'exactly one' fallback keeps single-business installs working
    without anyone setting active_business_id explicitly."""
    biz = active_business(session, founder)
    assert biz.id == business.id


def test_multiple_businesses_without_active_raises(
    session: Session, founder: Founder, business: Business
) -> None:
    second = Business(
        founder_id=founder.id, name="OtherCo", description=""
    )
    session.add(second)
    session.commit()
    with pytest.raises(BusinessResolutionError) as exc:
        active_business(session, founder)
    assert "no active one is selected" in str(exc.value)


def test_switch_active_then_resolve(
    session: Session, founder: Founder, business: Business
) -> None:
    second = create_business(session, founder, name="SecondCo")
    biz = switch_active(session, founder, second.id)
    assert biz.id == second.id
    # After switch, active resolves to the second.
    resolved = active_business(session, founder)
    assert resolved.id == second.id


def test_switch_to_other_founders_business_rejected(
    session: Session, founder: Founder, business: Business
) -> None:
    other = Founder(email="b@b", display_name="other")
    session.add(other)
    session.commit()
    other_biz = create_business(session, other, name="theirs")
    with pytest.raises(BusinessResolutionError):
        switch_active(session, founder, other_biz.id)


def test_create_with_set_active_updates_pointer(
    session: Session, founder: Founder
) -> None:
    biz = create_business(session, founder, name="brand-new", set_active=True)
    session.refresh(founder)
    assert founder.active_business_id == biz.id


def test_create_without_set_active_does_not_update_pointer(
    session: Session, founder: Founder, business: Business
) -> None:
    second = create_business(
        session, founder, name="side-project", set_active=False
    )
    session.refresh(founder)
    assert founder.active_business_id != second.id


def test_list_returns_newest_first(
    session: Session, founder: Founder, business: Business
) -> None:
    second = create_business(session, founder, name="newer")
    rows = list_businesses(session, founder.id)
    assert rows[0].id == second.id


def test_active_falls_back_when_pointer_dangling(
    session: Session, founder: Founder, business: Business
) -> None:
    """If active_business_id points at a deleted/unreachable business but
    the founder has exactly one valid business, fall back to that one."""
    from uuid import uuid4

    founder.active_business_id = uuid4()  # nonexistent
    session.add(founder)
    session.commit()
    biz = active_business(session, founder)
    assert biz.id == business.id
