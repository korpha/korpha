"""Tests for worker dispatch — Workforce routes specialized
tasks to hired workers via [WORKER:specialty] tags."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.director import (
    DEFAULT_PERSONALITIES, AttemptResult, Director, DirectorPersonality,
    Worker,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.cofounder.workforce import (
    Workforce, _worker_specialty_from_tag,
)
from korpha.identity.model import Founder


# ---- _worker_specialty_from_tag ----


def test_worker_tag_extracts_specialty() -> None:
    assert _worker_specialty_from_tag(
        "[WORKER:copywriter] write 3 LinkedIn posts"
    ) == "copywriter"


def test_worker_tag_lowercase() -> None:
    assert _worker_specialty_from_tag(
        "[worker:designer] design the hero section"
    ) == "designer"


def test_worker_tag_with_leading_whitespace() -> None:
    assert _worker_specialty_from_tag(
        "  [WORKER:support] reply to ticket"
    ) == "support"


def test_worker_tag_without_brackets_returns_none() -> None:
    assert _worker_specialty_from_tag("WORKER copywriter") is None


def test_worker_tag_no_colon_returns_none() -> None:
    """[WORKER] without a specialty isn't routed to a worker —
    the existing Hermes role-tag pattern handles bare [WORKER]."""
    assert _worker_specialty_from_tag(
        "[WORKER] do something vague"
    ) is None


def test_role_tag_not_worker_tag_returns_none() -> None:
    assert _worker_specialty_from_tag("[CTO] deploy") is None


# ---- select_executor ----


@pytest.fixture
def workforce(session: Session, business: Business, ceo: AgentRole):
    """Build a workforce with the three default C-suite
    Directors. We don't hire workers here — tests do that
    explicitly to keep state explicit."""
    from korpha.approvals.gate import ApprovalGate
    from korpha.blockers.queue import BlockerQueue
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    pool = InferencePool(providers=[], accounts=[])
    tracker = CostTracker(pool=pool)
    queue = BlockerQueue(session=session)
    hiring = HiringService(session)
    # Pre-hire so director.role_id_for() doesn't write rows.
    for role in (RoleType.CTO, RoleType.CMO, RoleType.COO):
        hiring.hire(
            business.id, role, title=role.value.upper(),
        )

    directors = {}
    for role, personality in DEFAULT_PERSONALITIES.items():
        if role not in (RoleType.CTO, RoleType.CMO, RoleType.COO):
            continue
        directors[role] = Director(
            personality=personality, session=session,
            cost_tracker=tracker, queue=queue, hiring=hiring,
        )
    return Workforce(directors=directors)


def test_select_executor_worker_tag_returns_worker(
    workforce: Workforce, business: Business,
) -> None:
    """[WORKER:copywriter] tag → workforce spawns + returns a Worker."""
    executor = workforce.select_executor(
        "[WORKER:copywriter] write 3 LinkedIn posts",
        business_id=business.id,
    )
    assert isinstance(executor, Worker)
    assert executor.personality.specialty == "copywriter"


def test_select_executor_falls_back_to_director_for_unknown_specialty(
    workforce: Workforce, business: Business,
) -> None:
    """Unknown specialty (no personality registered) → routes
    to the director the role tag would have picked, NOT a Worker."""
    executor = workforce.select_executor(
        "[WORKER:nonexistent] do something",
        business_id=business.id,
    )
    # No worker personality registered — falls through to
    # select_director, which falls through to fallback (CTO)
    # since there's no role tag and no keyword match.
    assert isinstance(executor, Director)


def test_select_executor_role_tag_when_no_worker_tag(
    workforce: Workforce, business: Business,
) -> None:
    executor = workforce.select_executor(
        "[CTO] deploy /pricing",
        business_id=business.id,
    )
    assert isinstance(executor, Director)
    assert executor.personality.role_type == RoleType.CTO


def test_select_executor_falls_through_to_keyword_routing(
    workforce: Workforce, business: Business,
) -> None:
    """No tags + obvious CMO domain keywords → CMO Director."""
    executor = workforce.select_executor(
        "write a tagline for our landing page",
        business_id=business.id,
    )
    assert isinstance(executor, Director)
    # 'tagline' + 'landing page' both score for CMO


# ---- _parse_task_role_tag (CEO mirror) ----


def test_parse_worker_tag_assigns_to_parent_role() -> None:
    from korpha.cofounder.ceo import _parse_task_role_tag

    owner, title = _parse_task_role_tag(
        "[WORKER:copywriter] draft 3 emails"
    )
    # copywriter's parent is CMO
    assert owner == "cmo"
    assert title == "draft 3 emails"


def test_parse_worker_tag_designer_assigns_to_cmo() -> None:
    from korpha.cofounder.ceo import _parse_task_role_tag

    owner, title = _parse_task_role_tag(
        "[WORKER:designer] mock the hero"
    )
    assert owner == "cmo"


def test_parse_worker_tag_support_assigns_to_coo() -> None:
    from korpha.cofounder.ceo import _parse_task_role_tag

    owner, title = _parse_task_role_tag(
        "[WORKER:support] reply to ticket #42"
    )
    assert owner == "coo"


def test_parse_worker_tag_unknown_specialty_no_owner() -> None:
    """A worker specialty with no registered personality lands
    as an unassigned card — Mike can specify_card it later."""
    from korpha.cofounder.ceo import _parse_task_role_tag

    owner, title = _parse_task_role_tag(
        "[WORKER:custom-specialty] do the thing"
    )
    assert owner is None
    assert title == "do the thing"


# ---- CEO team specialty hint ----


def test_ceo_propose_prompt_lists_active_workers(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    """The CEO's plan-prompt should mention which specialties are
    available so the model knows it can use [WORKER:foo] tags."""
    from korpha.approvals.gate import ApprovalGate
    from korpha.cofounder.ceo import CEO
    from korpha.cofounder.hiring import HiringService
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    hiring = HiringService(session)
    hiring.hire(
        business.id, RoleType.WORKER,
        title="Copywriter", specialty="copywriter",
    )
    hiring.hire(
        business.id, RoleType.WORKER,
        title="Designer", specialty="designer",
    )

    pool = InferencePool(providers=[], accounts=[])
    ceo_obj = CEO(
        session=session,
        cost_tracker=CostTracker(pool=pool),
        hiring=hiring,
        gate=ApprovalGate(session=session),
    )
    hint = ceo_obj._team_specialty_hint(business.id)
    assert "copywriter" in hint
    assert "designer" in hint
    assert "WORKER:specialty" in hint


def test_ceo_team_hint_empty_when_no_workers(
    session: Session, business: Business, ceo: AgentRole,
) -> None:
    from korpha.approvals.gate import ApprovalGate
    from korpha.cofounder.ceo import CEO
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    pool = InferencePool(providers=[], accounts=[])
    ceo_obj = CEO(
        session=session,
        cost_tracker=CostTracker(pool=pool),
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    hint = ceo_obj._team_specialty_hint(business.id)
    assert hint == ""


def test_ceo_team_hint_isolates_by_business(
    session: Session, business: Business, founder: Founder, ceo: AgentRole,
) -> None:
    """Workers hired against business A don't show up in B's hint."""
    other = Business(
        founder_id=founder.id, name="Other", description="",
    )
    session.add(other); session.commit(); session.refresh(other)
    HiringService(session).hire(
        other.id, RoleType.WORKER,
        title="Copywriter", specialty="copywriter",
    )

    from korpha.approvals.gate import ApprovalGate
    from korpha.cofounder.ceo import CEO
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    pool = InferencePool(providers=[], accounts=[])
    ceo_obj = CEO(
        session=session,
        cost_tracker=CostTracker(pool=pool),
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )
    hint = ceo_obj._team_specialty_hint(business.id)
    assert hint == ""
