"""Shared pytest fixtures."""
from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

import korpha.db.registry  # noqa: F401  -- registers all models on metadata
from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder


# Provider env vars the server reads to decide if an LLM backend is
# available. Tests that need a key configured set it explicitly via
# ``monkeypatch.setenv``; default state is "no provider" so the
# 503-when-no-provider tests assert the right behavior regardless of
# what's in the developer's shell or a loaded .env.
#
# Why autouse: without this, OPENCODE_API_KEY (or similar) leaking
# from .env causes the 503 tests to see 200 instead. The bug only
# surfaces in the full-suite run where some other test triggers
# .env loading; isolated runs spuriously pass. The autouse fixture
# locks that down for every test, including future ones that haven't
# been written yet.
_PROVIDER_ENV_VARS = (
    "OLLAMA_CLOUD_API_KEY",
    "OPENCODE_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "OLLAMA_PRO_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _scrub_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to "no provider configured" before every test.

    Tests that need a provider key set it via ``monkeypatch.setenv``
    inside the test body — monkeypatch undoes it on teardown so the
    next test starts clean again.
    """
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


@pytest.fixture
def founder(session: Session) -> Founder:
    f = Founder(email="mike@example.com", display_name="Mike")
    session.add(f)
    session.commit()
    session.refresh(f)
    return f


@pytest.fixture
def business(session: Session, founder: Founder) -> Business:
    b = Business(founder_id=founder.id, name="WidgetCo", description="B2B SaaS niche")
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


@pytest.fixture
def ceo(session: Session, business: Business) -> AgentRole:
    role = AgentRole(business_id=business.id, role_type=RoleType.CEO, title="CEO")
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@pytest.fixture
def cmo(session: Session, business: Business) -> AgentRole:
    role = AgentRole(business_id=business.id, role_type=RoleType.CMO, title="CMO")
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@pytest.fixture
def business_id(business: Business) -> UUID:
    return business.id
