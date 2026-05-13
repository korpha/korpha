"""Data model integration tests against SQLite."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

import korpha.db.registry  # noqa: F401  -- registers all models on metadata
from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
    AutonomyMode,
    TrustEnvelope,
)
from korpha.audit.model import Activity, ActorType, Cost, InferenceTier
from korpha.business.model import (
    Business,
    BusinessStatus,
    Goal,
    Project,
    ProjectStatus,
    Task,
    TaskStatus,
)
from korpha.cofounder.model import (
    AgentRole,
    Message,
    MessageSenderType,
    RoleType,
    Thread,
    ThreadPlatform,
)
from korpha.identity.model import Founder


@pytest.fixture
def engine() -> Iterator[Engine]:
    e = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


def _make_founder_and_business(session: Session) -> tuple[Founder, Business]:
    founder = Founder(email="mike@example.com", display_name="Mike")
    session.add(founder)
    session.commit()
    session.refresh(founder)

    business = Business(
        founder_id=founder.id,
        name="WidgetCo",
        description="B2B SaaS niche",
    )
    session.add(business)
    session.commit()
    session.refresh(business)
    return founder, business


def test_founder_unique_email(session: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    session.add(Founder(email="a@x.com"))
    session.commit()
    session.add(Founder(email="a@x.com"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_business_defaults_to_idea(session: Session) -> None:
    _, business = _make_founder_and_business(session)
    assert business.status == BusinessStatus.IDEA


def test_goal_hierarchy(session: Session) -> None:
    _, business = _make_founder_and_business(session)

    parent = Goal(business_id=business.id, title="Reach $5k MRR", target_metric="MRR", target_value=5000)
    session.add(parent)
    session.commit()
    session.refresh(parent)

    child = Goal(
        business_id=business.id,
        parent_goal_id=parent.id,
        title="Get 50 trial signups",
    )
    session.add(child)
    session.commit()

    assert child.parent_goal_id == parent.id


def test_project_task_chain(session: Session) -> None:
    _, business = _make_founder_and_business(session)

    project = Project(business_id=business.id, title="Launch landing page")
    session.add(project)
    session.commit()
    session.refresh(project)

    task = Task(business_id=business.id, project_id=project.id, title="Draft copy")
    session.add(task)
    session.commit()
    session.refresh(task)

    assert task.status == TaskStatus.PENDING
    assert task.project_id == project.id
    assert project.status == ProjectStatus.PLANNING


def test_agent_role_and_thread(session: Session) -> None:
    founder, business = _make_founder_and_business(session)

    ceo = AgentRole(business_id=business.id, role_type=RoleType.CEO, title="CEO")
    session.add(ceo)
    session.commit()
    session.refresh(ceo)

    thread = Thread(
        business_id=business.id,
        founder_id=founder.id,
        agent_role_id=ceo.id,
        platform=ThreadPlatform.WEB,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)

    msg = Message(
        thread_id=thread.id,
        sender_type=MessageSenderType.FOUNDER,
        content="What should I work on today?",
    )
    session.add(msg)
    session.commit()

    assert ceo.is_active
    assert thread.platform == ThreadPlatform.WEB
    assert msg.thread_id == thread.id


def test_approval_lifecycle(session: Session) -> None:
    _, business = _make_founder_and_business(session)
    cmo = AgentRole(business_id=business.id, role_type=RoleType.CMO, title="CMO")
    session.add(cmo)
    session.commit()
    session.refresh(cmo)

    approval = Approval(
        business_id=business.id,
        agent_role_id=cmo.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
        proposal_summary="Tweet about beta launch",
        action_payload={"text": "We're live!"},
    )
    session.add(approval)
    session.commit()
    session.refresh(approval)

    assert approval.status == ApprovalStatus.PENDING
    assert approval.action_payload == {"text": "We're live!"}


def test_trust_envelope_defaults(session: Session) -> None:
    _, business = _make_founder_and_business(session)
    env = TrustEnvelope(
        business_id=business.id,
        action_class=ActionClass.PUBLIC_POST,
        platform="twitter",
    )
    session.add(env)
    session.commit()
    session.refresh(env)

    assert env.threshold == 5
    assert env.mode == AutonomyMode.DRAFT
    assert env.consecutive_approvals == 0


def test_activity_log(session: Session) -> None:
    founder, business = _make_founder_and_business(session)
    a = Activity(
        business_id=business.id,
        actor_type=ActorType.FOUNDER,
        actor_id=founder.id,
        event_type="business.created",
        payload={"name": business.name},
    )
    session.add(a)
    session.commit()
    session.refresh(a)
    assert a.payload == {"name": "WidgetCo"}


def test_cost_decimal_precision(session: Session) -> None:
    from decimal import Decimal

    _, business = _make_founder_and_business(session)
    c = Cost(
        business_id=business.id,
        provider="deepseek",
        model="deepseek-v4-pro",
        tier=InferenceTier.PRO,
        input_tokens=1000,
        output_tokens=500,
        cached_tokens=400,
        cost_usd=Decimal("0.001234"),
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    assert c.cost_usd == Decimal("0.001234")
    assert c.tier == InferenceTier.PRO
