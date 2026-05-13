"""PR-INT-2 tests — memory.recall enforces namespace partition + grant.

Existing memory.recall tests cover the basic recall path; these
extend with namespace enforcement: cross-namespace access raises
without grant, succeeds with grant, post-search filtering keeps the
partition tight when the provider can't enforce it itself.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.cooperation.board import CooperationBoard
from korpha.cooperation.model import CooperationStatus
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.memory.model import LongTermMemoryEntry
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


def _ctx_with_unit(session, business, founder, unit_id):
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
        business_unit_id=unit_id,
    )


@pytest.fixture
def two_units(
    session: Session, business: Business,
) -> tuple[BusinessUnit, BusinessUnit]:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    a = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    b = board.create(
        business_id=business.id, name="POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    return a, b


def _seed_memory(session, business, founder, namespace_id, text):
    entry = LongTermMemoryEntry(
        business_id=business.id,
        founder_id=founder.id,
        text=text,
        namespace_id=namespace_id,
    )
    session.add(entry)
    session.commit()
    return entry


@pytest.mark.asyncio
async def test_own_namespace_recall_succeeds(
    session: Session, business: Business, founder: Founder,
    two_units,
) -> None:
    """Caller scoped to unit A recalls own namespace's entries."""
    a, _ = two_units
    _seed_memory(session, business, founder, a.memory_namespace_id, "stripe key")
    skill = default_registry.skills["memory.recall"]
    out = await skill.run(
        ctx=_ctx_with_unit(session, business, founder, a.id),
        args={"query": "stripe"},
    )
    results = out.payload["results"]
    assert len(results) == 1


@pytest.mark.asyncio
async def test_foreign_namespace_recall_blocked_without_grant(
    session: Session, business: Business, founder: Founder,
    two_units,
) -> None:
    """Asking for unit B's namespace from unit A → SkillError."""
    a, b = two_units
    _seed_memory(session, business, founder, b.memory_namespace_id, "b-secret")
    skill = default_registry.skills["memory.recall"]
    with pytest.raises(SkillError, match="cross-namespace"):
        await skill.run(
            ctx=_ctx_with_unit(session, business, founder, a.id),
            args={
                "query": "b-secret",
                "namespace_id": str(b.memory_namespace_id),
            },
        )


@pytest.mark.asyncio
async def test_foreign_namespace_recall_passes_with_grant(
    session: Session, business: Business, founder: Founder,
    two_units,
) -> None:
    """ACCEPTED CooperationProposal with cross_namespace_recall lets
    unit A read unit B's namespace."""
    a, b = two_units
    _seed_memory(session, business, founder, b.memory_namespace_id, "b-shared")
    coop = CooperationBoard(session)
    prop = coop.propose(
        business_id=business.id,
        from_unit_id=a.id, to_unit_id=b.id,
        summary="share editorial notes",
        permissions={"cross_namespace_recall": True},
    )
    coop.decide(prop.id, decision=CooperationStatus.ACCEPTED)

    skill = default_registry.skills["memory.recall"]
    out = await skill.run(
        ctx=_ctx_with_unit(session, business, founder, a.id),
        args={
            "query": "b-shared",
            "namespace_id": str(b.memory_namespace_id),
        },
    )
    assert len(out.payload["results"]) == 1


@pytest.mark.asyncio
async def test_post_search_filter_excludes_other_namespace_rows(
    session: Session, business: Business, founder: Founder,
    two_units,
) -> None:
    """Even when the provider returns matching rows from another
    namespace, the recall skill filters them out (partition enforced
    at the skill layer)."""
    a, b = two_units
    _seed_memory(session, business, founder, a.memory_namespace_id, "shared keyword own")
    _seed_memory(session, business, founder, b.memory_namespace_id, "shared keyword other")
    skill = default_registry.skills["memory.recall"]
    out = await skill.run(
        ctx=_ctx_with_unit(session, business, founder, a.id),
        args={"query": "shared keyword"},
    )
    # Only the own-namespace match survives the filter
    results = out.payload["results"]
    assert len(results) == 1
    assert "own" in results[0]["text"]


@pytest.mark.asyncio
async def test_no_unit_context_caller_sees_all_for_back_compat(
    session: Session, business: Business, founder: Founder,
    two_units,
) -> None:
    """Pre-PR9 caller (no business_unit_id on SkillContext) sees
    entries regardless of namespace — back-compat for legacy callers.
    Tightening to require unit context happens in a future migration."""
    a, b = two_units
    _seed_memory(session, business, founder, a.memory_namespace_id, "namespace-alpha entry")
    _seed_memory(session, business, founder, b.memory_namespace_id, "namespace-beta entry")
    skill = default_registry.skills["memory.recall"]
    out = await skill.run(
        ctx=SkillContext(
            business=business, founder=founder, session=session,
            cost_tracker=CostTracker(pool=InferencePool(
                providers=[], accounts=[],
            )),
        ),
        args={"query": "namespace entry"},
    )
    # Without unit context, target_ns is None → no filtering applied
    assert len(out.payload["results"]) == 2


@pytest.mark.asyncio
async def test_unknown_foreign_namespace_with_no_own_unit_raises(
    session: Session, business: Business, founder: Founder,
    two_units,
) -> None:
    """If caller has no unit context AND requests a foreign namespace,
    the skill refuses (can't authorize a foreign read without an
    asking unit)."""
    _, b = two_units
    _seed_memory(session, business, founder, b.memory_namespace_id, "x")
    skill = default_registry.skills["memory.recall"]
    with pytest.raises(SkillError, match="no unit context"):
        await skill.run(
            ctx=SkillContext(
                business=business, founder=founder, session=session,
                cost_tracker=CostTracker(pool=InferencePool(
                    providers=[], accounts=[],
                )),
            ),
            args={
                "query": "x",
                "namespace_id": str(b.memory_namespace_id),
            },
        )
