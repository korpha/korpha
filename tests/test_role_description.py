"""Tests for AgentRole.description field + CEO routing hint."""
from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder


@pytest.fixture()
def engine():
    import korpha.db.registry  # noqa: F401
    e = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(e)
    return e


@pytest.fixture()
def business(engine):
    with Session(engine) as s:
        f = Founder(email="m@x.com")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(founder_id=f.id, name="Test")
        s.add(b); s.commit(); s.refresh(b)
        return b.id


def test_hire_stores_description(engine, business):
    with Session(engine) as s:
        role = HiringService(s).hire(
            business, RoleType.WORKER,
            specialty="copywriter",
            description="Writes punchy 60-word tweets. Voice = casual, no emoji.",
        )
        assert role.description.startswith("Writes punchy")


def test_hire_description_optional(engine, business):
    with Session(engine) as s:
        role = HiringService(s).hire(
            business, RoleType.WORKER, specialty="designer",
        )
        assert role.description is None


def test_team_hint_uses_description_for_disambiguation(engine, business):
    """When two workers share a specialty, the CEO hint should
    surface each one's description so the LLM can pick correctly."""
    from korpha.cofounder.ceo import CEO

    with Session(engine) as s:
        hr = HiringService(s)
        hr.hire(
            business, RoleType.WORKER,
            specialty="copywriter",
            title="Tweet Punchwriter",
            description="60-word punchy tweets. Voice: casual.",
        )
        hr.hire(
            business, RoleType.WORKER,
            specialty="copywriter",
            title="Long-form Teardown",
            description="800-word teardown blog posts. Voice: analytical.",
        )

        # Build a CEO instance just enough to call the hint method.
        from unittest.mock import MagicMock
        ceo = CEO.__new__(CEO)
        ceo.session = s
        hint = ceo._team_specialty_hint(business)
        assert "Tweet Punchwriter" in hint
        assert "Long-form Teardown" in hint
        assert "60-word punchy" in hint
        assert "800-word teardown" in hint


def test_team_hint_keyword_only_when_single_worker_no_desc(engine, business):
    """One worker, no description → hint stays compact (just the
    specialty), not bloated with empty fields."""
    from korpha.cofounder.ceo import CEO

    with Session(engine) as s:
        HiringService(s).hire(
            business, RoleType.WORKER, specialty="designer",
        )
        ceo = CEO.__new__(CEO)
        ceo.session = s
        hint = ceo._team_specialty_hint(business)
        assert "designer" in hint
        assert "Description" not in hint  # no per-worker breakdown


def test_team_hint_empty_when_no_workers(engine, business):
    from korpha.cofounder.ceo import CEO

    with Session(engine) as s:
        ceo = CEO.__new__(CEO)
        ceo.session = s
        assert ceo._team_specialty_hint(business) == ""


def test_long_description_truncated_in_hint(engine, business):
    """Routing hint truncates >180-char descriptions so multi-worker
    teams don't balloon the CEO prompt."""
    from korpha.cofounder.ceo import CEO

    with Session(engine) as s:
        hr = HiringService(s)
        hr.hire(
            business, RoleType.WORKER, specialty="ads",
            title="A", description="A" * 500,
        )
        hr.hire(
            business, RoleType.WORKER, specialty="ads",
            title="B", description="B" * 500,
        )
        ceo = CEO.__new__(CEO)
        ceo.session = s
        hint = ceo._team_specialty_hint(business)
        # 180 chars + ... = 183
        assert "A" * 500 not in hint
        assert "..." in hint
