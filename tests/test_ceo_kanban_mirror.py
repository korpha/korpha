"""CEO Plan → kanban mirror tests."""
from __future__ import annotations

from sqlmodel import Session, select

from korpha.cofounder.ceo import (
    Plan, _parse_task_role_tag,
)
from korpha.kanban.model import KanbanCard


# ---- _parse_task_role_tag ----


def test_role_tag_cto() -> None:
    owner, title = _parse_task_role_tag("[CTO] build the landing page")
    assert owner == "cto"
    assert title == "build the landing page"


def test_role_tag_cmo() -> None:
    owner, title = _parse_task_role_tag("[CMO] write 3 LinkedIn posts")
    assert owner == "cmo"
    assert title == "write 3 LinkedIn posts"


def test_role_tag_coo() -> None:
    owner, title = _parse_task_role_tag("[COO] set up Stripe webhook")
    assert owner == "coo"


def test_role_tag_lowercase() -> None:
    owner, title = _parse_task_role_tag("[cto] x")
    assert owner == "cto"


def test_role_tag_missing_returns_none_owner() -> None:
    owner, title = _parse_task_role_tag("just a task with no tag")
    assert owner is None
    assert title == "just a task with no tag"


def test_role_tag_unknown_role_returns_none() -> None:
    """[CIO] isn't a known role — keep the task but don't assign."""
    owner, title = _parse_task_role_tag("[CIO] do something")
    assert owner is None
    # we still strip the bracket so the title is clean
    assert "[CIO]" not in title


def test_role_tag_ceo_returns_none_owner() -> None:
    """CEO tag means the CEO will keep handling — no execution owner."""
    owner, title = _parse_task_role_tag("[CEO] check with founder")
    assert owner is None


def test_role_tag_empty() -> None:
    owner, title = _parse_task_role_tag("")
    assert owner is None
    assert title == ""


def test_role_tag_with_colon() -> None:
    owner, title = _parse_task_role_tag("[CTO]: deploy")
    assert owner == "cto"
    assert title == "deploy"


# ---- mirror integration ----


def _stub_ceo(session: Session, business, ceo_role):
    """Build a CEO without touching inference. We only exercise the
    mirror helper directly."""
    from korpha.cofounder.ceo import CEO
    from korpha.cofounder.hiring import HiringService
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    from korpha.approvals.gate import ApprovalGate

    pool = InferencePool(providers=[], accounts=[])
    return CEO(
        session=session,
        cost_tracker=CostTracker(pool=pool),
        hiring=HiringService(session),
        gate=ApprovalGate(session=session),
    )


def test_mirror_plan_writes_one_card_per_task(
    session: Session, business, founder, ceo,
) -> None:
    ceo_obj = _stub_ceo(session, business, ceo)
    plan = Plan(
        summary="Launch the new pricing page",
        rationale=["test"],
        next_action="ship it",
        tasks=[
            "[CTO] deploy /pricing route",
            "[CMO] write the headline",
            "[COO] update support docs",
        ],
        estimated_hours=6.0,
        expected_impact="more conversions",
        requires_founder_approval=False,
        reasoning=None,
        raw_response="",
    )
    ceo_obj._mirror_plan_to_kanban(
        business_id=business.id,
        ceo_role_id=ceo.id,
        founder_id=founder.id,
        plan=plan,
    )

    cards = list(session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).all())
    assert len(cards) == 3
    by_title = {c.title: c for c in cards}
    assert "deploy /pricing route" in by_title
    assert by_title["deploy /pricing route"].owner_role == "cto"
    assert by_title["write the headline"].owner_role == "cmo"
    assert by_title["update support docs"].owner_role == "coo"
    # all in BACKLOG
    for c in cards:
        assert c.column.value == "backlog"


def test_mirror_plan_attaches_summary_to_body(
    session: Session, business, founder, ceo,
) -> None:
    ceo_obj = _stub_ceo(session, business, ceo)
    plan = Plan(
        summary="ship the demo video",
        rationale=[],
        next_action="record voiceover",
        tasks=["[CTO] cut the demo"],
        estimated_hours=2.0,
        expected_impact="",
        requires_founder_approval=False,
        reasoning=None,
        raw_response="",
    )
    ceo_obj._mirror_plan_to_kanban(
        business_id=business.id,
        ceo_role_id=ceo.id,
        founder_id=founder.id,
        plan=plan,
    )
    card = session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).one()
    assert "ship the demo video" in card.body
    assert "record voiceover" in card.body


def test_mirror_plan_with_no_tasks_creates_no_cards(
    session: Session, business, founder, ceo,
) -> None:
    ceo_obj = _stub_ceo(session, business, ceo)
    plan = Plan(
        summary="x", rationale=[], next_action="ask Mike",
        tasks=[], estimated_hours=None, expected_impact="",
        requires_founder_approval=False, reasoning=None, raw_response="",
    )
    ceo_obj._mirror_plan_to_kanban(
        business_id=business.id,
        ceo_role_id=ceo.id,
        founder_id=founder.id,
        plan=plan,
    )
    cards = list(session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).all())
    assert cards == []


def test_mirror_plan_skips_blank_tasks(
    session: Session, business, founder, ceo,
) -> None:
    """Empty / whitespace-only entries don't produce cards."""
    ceo_obj = _stub_ceo(session, business, ceo)
    plan = Plan(
        summary="x", rationale=[], next_action="",
        tasks=["", "   ", "[CTO] real task"],
        estimated_hours=None, expected_impact="",
        requires_founder_approval=False, reasoning=None, raw_response="",
    )
    ceo_obj._mirror_plan_to_kanban(
        business_id=business.id,
        ceo_role_id=ceo.id,
        founder_id=founder.id,
        plan=plan,
    )
    cards = list(session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).all())
    assert len(cards) == 1
    assert cards[0].title == "real task"


def test_mirror_plan_handles_untagged_task(
    session: Session, business, founder, ceo,
) -> None:
    """Untagged tasks are kept but have no owner."""
    ceo_obj = _stub_ceo(session, business, ceo)
    plan = Plan(
        summary="x", rationale=[], next_action="",
        tasks=["plain task no tag"],
        estimated_hours=None, expected_impact="",
        requires_founder_approval=False, reasoning=None, raw_response="",
    )
    ceo_obj._mirror_plan_to_kanban(
        business_id=business.id,
        ceo_role_id=ceo.id,
        founder_id=founder.id,
        plan=plan,
    )
    card = session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).one()
    assert card.title == "plain task no tag"
    assert card.owner_role is None


def test_mirror_plan_links_provenance_to_ceo_and_founder(
    session: Session, business, founder, ceo,
) -> None:
    ceo_obj = _stub_ceo(session, business, ceo)
    plan = Plan(
        summary="x", rationale=[], next_action="",
        tasks=["[CTO] task"],
        estimated_hours=None, expected_impact="",
        requires_founder_approval=False, reasoning=None, raw_response="",
    )
    ceo_obj._mirror_plan_to_kanban(
        business_id=business.id,
        ceo_role_id=ceo.id,
        founder_id=founder.id,
        plan=plan,
    )
    card = session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).one()
    assert card.created_by_agent_role_id == ceo.id
    assert card.created_by_founder_id == founder.id
