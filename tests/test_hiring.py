"""HiringService tests."""
from __future__ import annotations

from sqlmodel import Session, select

from korpha.audit.model import Activity
from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService, HiringTrigger
from korpha.cofounder.model import AgentRole, RoleType


def test_ensure_ceo_creates_when_missing(session: Session, business: Business) -> None:
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    assert ceo.role_type == RoleType.CEO
    assert ceo.is_active


def test_ensure_ceo_idempotent(session: Session, business: Business) -> None:
    hiring = HiringService(session)
    a = hiring.ensure_ceo(business.id)
    b = hiring.ensure_ceo(business.id)
    assert a.id == b.id


def test_trigger_hires_cto_on_first_build_task(
    session: Session, business: Business
) -> None:
    hiring = HiringService(session)
    cto = hiring.trigger_hire_if_needed(
        business.id, HiringTrigger.BUILD_TASK_CREATED
    )
    assert cto is not None
    assert cto.role_type == RoleType.CTO


def test_trigger_idempotent(session: Session, business: Business) -> None:
    """Second trigger of the same kind doesn't double-hire."""
    hiring = HiringService(session)
    first = hiring.trigger_hire_if_needed(
        business.id, HiringTrigger.LAUNCH_PLAN_CREATED
    )
    second = hiring.trigger_hire_if_needed(
        business.id, HiringTrigger.LAUNCH_PLAN_CREATED
    )
    assert first is not None
    assert second is None  # already hired


def test_fire_marks_role_inactive(session: Session, business: Business) -> None:
    hiring = HiringService(session)
    cmo = hiring.hire(business.id, RoleType.CMO)
    fired = hiring.fire(cmo.id, reason="restructuring")
    assert not fired.is_active
    assert fired.fired_at is not None


def test_rehire_after_fire(session: Session, business: Business) -> None:
    """After firing CMO, we can hire a new one."""
    hiring = HiringService(session)
    cmo_v1 = hiring.hire(business.id, RoleType.CMO)
    hiring.fire(cmo_v1.id)
    cmo_v2 = hiring.hire(business.id, RoleType.CMO)
    assert cmo_v2.id != cmo_v1.id
    assert cmo_v2.is_active


def test_workers_can_have_multiple_active(
    session: Session, business: Business
) -> None:
    hiring = HiringService(session)
    designer = hiring.hire(business.id, RoleType.WORKER, specialty="designer")
    copywriter = hiring.hire(business.id, RoleType.WORKER, specialty="copywriter")
    assert designer.id != copywriter.id
    assert designer.is_active and copywriter.is_active


def test_hire_activity_log(session: Session, business: Business) -> None:
    hiring = HiringService(session)
    hiring.ensure_ceo(business.id)
    activities = session.exec(
        select(Activity).where(Activity.business_id == business.id)
    ).all()
    assert any(a.event_type == "agent.hired" for a in activities)


def test_only_one_active_per_csuite_role(
    session: Session, business: Business
) -> None:
    """Calling hire twice for the same C-suite role returns the same record."""
    hiring = HiringService(session)
    a = hiring.hire(business.id, RoleType.CTO)
    b = hiring.hire(business.id, RoleType.CTO)
    assert a.id == b.id

    actives = session.exec(
        select(AgentRole)
        .where(AgentRole.business_id == business.id)
        .where(AgentRole.role_type == RoleType.CTO)
        .where(AgentRole.is_active == True)  # noqa: E712
    ).all()
    assert len(actives) == 1
