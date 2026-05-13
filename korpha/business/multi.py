"""Multi-business helpers: pick the active business, list, create, switch.

Supports the case where a Founder runs more than one business through the
same Korpha install — e.g. Mike's main Substack plus a side weekend
no-code experiment. Each business has its own threads, blockers, agents,
and trust envelopes; switching is just a pointer update on the Founder
row, no data migration.

The schema has always carried ``business_id`` foreign keys, so the data
model already supports many-per-founder. This module is the glue that
lets the CLI / API target a specific business cleanly.
"""
from __future__ import annotations

from uuid import UUID

from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.identity.model import Founder


class BusinessResolutionError(LookupError):
    """No business matched the lookup, or the Founder has zero businesses."""


def list_businesses(session: Session, founder_id: UUID) -> list[Business]:
    """All businesses for this Founder, newest first. Includes archived."""
    return list(
        session.exec(
            select(Business)
            .where(Business.founder_id == founder_id)
            .order_by(Business.created_at.desc())  # type: ignore[attr-defined]
        ).all()
    )


def active_business(session: Session, founder: Founder) -> Business:
    """Return the Founder's currently active business.

    Resolution order:
      1. If ``founder.active_business_id`` is set and the row exists → that.
      2. If the Founder has exactly one business → that.
      3. Otherwise raise BusinessResolutionError.

    The "exactly one" fallback keeps single-business installs working
    without anyone calling switch_active() — important for backward
    compatibility with the OSS install where the default founder created
    by `korpha init` only ever has one business.
    """
    if founder.active_business_id is not None:
        biz = session.get(Business, founder.active_business_id)
        if biz is not None and biz.founder_id == founder.id:
            return biz

    rows = list_businesses(session, founder.id)
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise BusinessResolutionError(
            f"founder {founder.email!r} has no businesses — run "
            "`korpha business-create` to make one"
        )
    raise BusinessResolutionError(
        f"founder {founder.email!r} owns {len(rows)} businesses but no "
        "active one is selected — run `korpha business-switch <id>`"
    )


def create_business(
    session: Session,
    founder: Founder,
    *,
    name: str,
    description: str | None = None,
    set_active: bool = False,
) -> Business:
    """Insert a new business. Returns the persisted row.

    ``set_active=True`` updates the founder's pointer in the same
    transaction so the CLI doesn't have to do two round trips.
    """
    biz = Business(
        founder_id=founder.id,
        name=name.strip(),
        description=(description or "").strip() or None,
    )
    session.add(biz)
    session.commit()
    session.refresh(biz)
    if set_active:
        founder.active_business_id = biz.id
        session.add(founder)
        session.commit()
    return biz


def switch_active(
    session: Session,
    founder: Founder,
    business_id: UUID,
) -> Business:
    """Point the Founder's ``active_business_id`` at this business. Raises
    BusinessResolutionError if the business doesn't belong to them."""
    biz = session.get(Business, business_id)
    if biz is None or biz.founder_id != founder.id:
        raise BusinessResolutionError(
            f"business {business_id} not found for founder {founder.email!r}"
        )
    founder.active_business_id = biz.id
    session.add(founder)
    session.commit()
    return biz


__all__ = [
    "BusinessResolutionError",
    "active_business",
    "create_business",
    "list_businesses",
    "switch_active",
]
