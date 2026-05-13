"""Business portability: export, import, secret scrub, ID regeneration."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
)
from korpha.audit.model import Activity, ActorType
from korpha.blockers.model import Blocker, BlockerKind, BlockerUrgency
from korpha.business.model import Business, Goal
from korpha.business.portability import (
    PortabilityError,
    export_business,
    export_to_file,
    import_business,
    import_from_file,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import (
    Message,
    MessageSenderType,
    Thread,
    ThreadPlatform,
)
from korpha.heartbeats.model import Routine, RoutineSchedule
from korpha.identity.model import Founder


def _seed_full_business(session: Session, founder: Founder) -> Business:
    biz = Business(
        founder_id=founder.id,
        name="Source Co",
        description="seeded for export tests",
    )
    session.add(biz)
    session.commit()
    session.refresh(biz)

    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(biz.id)

    goal = Goal(business_id=biz.id, title="ship landing page", target_value=1)
    session.add(goal)

    thread = Thread(
        business_id=biz.id,
        founder_id=founder.id,
        agent_role_id=ceo.id,
        platform=ThreadPlatform.WEB,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)

    session.add(
        Message(
            thread_id=thread.id,
            sender_type=MessageSenderType.FOUNDER,
            content="hi cofounder",
        )
    )
    session.add(
        Blocker(
            business_id=biz.id,
            requesting_agent_role_id=ceo.id,
            kind=BlockerKind.DECISION,
            urgency=BlockerUrgency.NORMAL,
            title="pick brand color",
            detail="need a hex",
        )
    )
    session.add(
        Approval(
            business_id=biz.id,
            agent_role_id=ceo.id,
            action_class=ActionClass.PUBLIC_POST,
            platform="twitter",
            proposal_summary="post a cold opener",
            action_payload={"text": "hi", "secret_token": "abc123"},
            status=ApprovalStatus.PENDING,
        )
    )
    session.add(
        Activity(
            business_id=biz.id,
            actor_type=ActorType.AGENT,
            event_type="ceo.responded",
            payload={},
        )
    )
    session.add(
        Routine(
            business_id=biz.id,
            name="daily digest",
            kind="ceo.daily_digest",
            schedule_kind=RoutineSchedule.EVERY_SECONDS,
            schedule_value=86400,
        )
    )
    session.commit()
    return biz


def test_export_returns_payload_with_counts(
    session: Session, founder: Founder
) -> None:
    biz = _seed_full_business(session, founder)
    result = export_business(session, business_id=biz.id)
    assert result.payload["business"]["name"] == "Source Co"
    assert result.payload["format_version"] >= 1
    assert result.table_counts["goals"] == 1
    assert result.table_counts["messages"] == 1
    assert result.table_counts["blockers"] == 1


def test_export_scrubs_secret_keys_from_approval(
    session: Session, founder: Founder
) -> None:
    biz = _seed_full_business(session, founder)
    result = export_business(session, business_id=biz.id)
    [approval] = result.payload["approvals"]
    payload = approval["action_payload"]
    assert "text" in payload
    assert "secret_token" not in payload  # scrubbed


def test_export_excludes_messages_when_disabled(
    session: Session, founder: Founder
) -> None:
    biz = _seed_full_business(session, founder)
    result = export_business(session, business_id=biz.id, include_messages=False)
    assert result.table_counts["threads"] == 0
    assert result.table_counts["messages"] == 0


def test_unknown_business_raises(session: Session) -> None:
    with pytest.raises(PortabilityError):
        export_business(session, business_id=uuid4())


def test_round_trip_creates_new_uuids(
    session: Session, founder: Founder
) -> None:
    biz = _seed_full_business(session, founder)
    payload = export_business(session, business_id=biz.id).payload

    result = import_business(session, payload, founder=founder, new_name="Clone")
    assert result.business.id != biz.id  # new UUID
    assert result.business.name == "Clone"
    assert result.business.founder_id == founder.id

    # Counts on the cloned side match the source.
    cloned_msgs = list(
        session.exec(
            select(Message).join(Thread).where(Thread.business_id == result.business.id)
        ).all()
    )
    assert len(cloned_msgs) == 1
    cloned_blockers = list(
        session.exec(select(Blocker).where(Blocker.business_id == result.business.id)).all()
    )
    assert len(cloned_blockers) == 1


def test_round_trip_preserves_goal_target_metric(
    session: Session, founder: Founder
) -> None:
    biz = _seed_full_business(session, founder)
    payload = export_business(session, business_id=biz.id).payload
    result = import_business(session, payload, founder=founder)
    [cloned_goal] = session.exec(
        select(Goal).where(Goal.business_id == result.business.id)
    ).all()
    assert cloned_goal.title == "ship landing page"
    assert cloned_goal.target_value == 1


def test_import_can_replay_same_payload(
    session: Session, founder: Founder
) -> None:
    """Same payload imported twice → two distinct businesses, no PK clash."""
    biz = _seed_full_business(session, founder)
    payload = export_business(session, business_id=biz.id).payload
    a = import_business(session, payload, founder=founder, new_name="copy A")
    b = import_business(session, payload, founder=founder, new_name="copy B")
    assert a.business.id != b.business.id


def test_unsupported_version_raises(
    session: Session, founder: Founder
) -> None:
    payload = {
        "format_version": 99,
        "business": {"id": str(uuid4()), "name": "x"},
    }
    with pytest.raises(PortabilityError):
        import_business(session, payload, founder=founder)


def test_missing_business_object_raises(
    session: Session, founder: Founder
) -> None:
    payload = {"format_version": 1}
    with pytest.raises(PortabilityError):
        import_business(session, payload, founder=founder)


def test_export_to_file_then_import_from_file(
    session: Session, founder: Founder, tmp_path: Path
) -> None:
    biz = _seed_full_business(session, founder)
    out = tmp_path / "export.json"
    export_to_file(session, business_id=biz.id, path=str(out))
    assert out.exists()

    other = Founder(email="other@b", display_name="other")
    session.add(other)
    session.commit()
    result = import_from_file(session, path=str(out), founder=other)
    assert result.business.founder_id == other.id


def test_remap_preserves_internal_links(
    session: Session, founder: Founder
) -> None:
    """Threads should still point at the cloned agent_role, not the source's."""
    biz = _seed_full_business(session, founder)
    payload = export_business(session, business_id=biz.id).payload
    result = import_business(session, payload, founder=founder)
    cloned_threads = list(
        session.exec(
            select(Thread).where(Thread.business_id == result.business.id)
        ).all()
    )
    assert len(cloned_threads) == 1
    # The cloned thread's agent_role_id must point at a row in the cloned
    # business, not the source.
    from korpha.cofounder.model import AgentRole

    role = session.get(AgentRole, cloned_threads[0].agent_role_id)
    assert role is not None
    assert role.business_id == result.business.id
