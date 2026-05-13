"""PR-INT-9 tests — unit name/UUID resolver + auto-default skill args.

Covers:
- resolve_unit_id direct (UUID passthrough, name lookup, case
  insensitivity, fuzzy prefix, error paths)
- memory.remember accepts ``business_unit_id`` arg with name OR UUID
- cooperation.ask_about auto-defaults ``from_unit_id`` from
  ctx.business_unit_id, accepts name strings for to_unit_id
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.context import resolve_unit_id
from korpha.business_units.model import BusinessUnit, BusinessUnitKind
from korpha.cooperation.model import CrossUnitQueryLog
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.memory.model import LongTermMemoryEntry
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


@pytest.fixture
def tree(session: Session, business: Business) -> dict[str, BusinessUnit]:
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="Marketro",
        kind=BusinessUnitKind.DEFAULT,
    )
    kdp = board.create(
        business_id=business.id, name="Romance KDP",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    pod = board.create(
        business_id=business.id, name="Merch POD",
        kind=BusinessUnitKind.LINE, parent_id=root.id,
    )
    return {"root": root, "kdp": kdp, "pod": pod}


def _ctx(session, business, founder, unit_id=None) -> SkillContext:
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(
            pool=InferencePool(providers=[], accounts=[]),
        ),
        business_unit_id=unit_id,
    )


# ----------------------------------------------------------------------------
# resolve_unit_id direct
# ----------------------------------------------------------------------------


def test_resolve_unit_id_passes_uuid_through(
    session: Session, business: Business, tree: dict[str, BusinessUnit],
) -> None:
    out = resolve_unit_id(session, business.id, tree["kdp"].id)
    assert out == tree["kdp"].id


def test_resolve_unit_id_passes_uuid_string_through(
    session: Session, business: Business, tree: dict[str, BusinessUnit],
) -> None:
    out = resolve_unit_id(session, business.id, str(tree["kdp"].id))
    assert out == tree["kdp"].id


def test_resolve_unit_id_resolves_name(
    session: Session, business: Business, tree: dict[str, BusinessUnit],
) -> None:
    assert resolve_unit_id(session, business.id, "Romance KDP") == tree["kdp"].id


def test_resolve_unit_id_case_insensitive(
    session: Session, business: Business, tree: dict[str, BusinessUnit],
) -> None:
    assert resolve_unit_id(session, business.id, "romance kdp") == tree["kdp"].id
    assert resolve_unit_id(session, business.id, "MERCH POD") == tree["pod"].id


def test_resolve_unit_id_fuzzy_prefix(
    session: Session, business: Business, tree: dict[str, BusinessUnit],
) -> None:
    """A unique prefix resolves; multiple matches require exact name."""
    assert resolve_unit_id(session, business.id, "Romance") == tree["kdp"].id
    assert resolve_unit_id(session, business.id, "Merch") == tree["pod"].id


def test_resolve_unit_id_none_returns_none(
    session: Session, business: Business,
) -> None:
    assert resolve_unit_id(session, business.id, None) is None


def test_resolve_unit_id_empty_string_returns_none(
    session: Session, business: Business,
) -> None:
    assert resolve_unit_id(session, business.id, "   ") is None


def test_resolve_unit_id_missing_uuid_raises(
    session: Session, business: Business, tree: dict[str, BusinessUnit],
) -> None:
    with pytest.raises(ValueError, match="not found in this business"):
        resolve_unit_id(session, business.id, uuid4())


def test_resolve_unit_id_unknown_name_raises_with_available(
    session: Session, business: Business, tree: dict[str, BusinessUnit],
) -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve_unit_id(session, business.id, "Nonexistent Line")
    msg = str(exc_info.value)
    assert "No BusinessUnit named" in msg
    # error lists what IS available so the LLM can self-correct
    assert "Romance KDP" in msg or "Merch POD" in msg


def test_resolve_unit_id_isolates_across_businesses(
    session: Session, founder: Founder, business: Business,
    tree: dict[str, BusinessUnit],
) -> None:
    """A unit from business A is not findable when searching business B."""
    other = Business(
        founder_id=founder.id, name="OtherCo",
        description="other", founder_brief={},
    )
    session.add(other)
    session.commit()
    session.refresh(other)
    # The KDP UUID exists, but for a different business — should fail
    with pytest.raises(ValueError, match="not found in this business"):
        resolve_unit_id(session, other.id, tree["kdp"].id)


# ----------------------------------------------------------------------------
# memory.remember with business_unit_id arg
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_remember_accepts_unit_name(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    skill = default_registry.skills["memory.remember"]
    r = await skill.run(
        ctx=_ctx(session, business, founder, unit_id=None),
        args={
            "text": "Highland Rogue book 3 launches in June",
            "business_unit_id": "Romance KDP",
        },
    )
    assert r.payload["scoped_to_unit"] == "Romance KDP"
    assert r.payload["namespace_id"] == str(tree["kdp"].memory_namespace_id)
    # Persisted with the right namespace
    entries = list(session.exec(select(LongTermMemoryEntry)).all())
    assert len(entries) == 1
    assert entries[0].namespace_id == tree["kdp"].memory_namespace_id


@pytest.mark.asyncio
async def test_memory_remember_accepts_unit_uuid(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    skill = default_registry.skills["memory.remember"]
    r = await skill.run(
        ctx=_ctx(session, business, founder, unit_id=None),
        args={
            "text": "POD t-shirt vendor onboarded",
            "business_unit_id": str(tree["pod"].id),
        },
    )
    assert r.payload["scoped_to_unit"] == "Merch POD"


@pytest.mark.asyncio
async def test_memory_remember_arg_overrides_ctx(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """When BOTH ctx.business_unit_id and the arg are set, the arg wins."""
    skill = default_registry.skills["memory.remember"]
    r = await skill.run(
        ctx=_ctx(session, business, founder, unit_id=tree["pod"].id),
        args={
            "text": "test entry",
            "business_unit_id": "Romance KDP",  # override
        },
    )
    # Should land in KDP's namespace, not POD's
    assert r.payload["scoped_to_unit"] == "Romance KDP"


@pytest.mark.asyncio
async def test_memory_remember_unknown_unit_name_errors(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    skill = default_registry.skills["memory.remember"]
    with pytest.raises(SkillError, match="No BusinessUnit named"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"text": "x", "business_unit_id": "Fictional Line"},
        )


@pytest.mark.asyncio
async def test_memory_remember_no_unit_arg_no_ctx_stays_companywide(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """Back-compat: no unit arg + no ctx unit → namespace stays None
    (company-wide)."""
    skill = default_registry.skills["memory.remember"]
    r = await skill.run(
        ctx=_ctx(session, business, founder, unit_id=None),
        args={"text": "company-wide fact"},
    )
    assert r.payload["scoped_to_unit"] is None
    assert r.payload["namespace_id"] is None


# ----------------------------------------------------------------------------
# cooperation.ask_about auto-defaults + name resolution
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_about_accepts_unit_names(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    skill = default_registry.skills["cooperation.ask_about"]
    r = await skill.run(
        ctx=_ctx(session, business, founder, unit_id=tree["kdp"].id),
        args={
            "from_unit_id": "Romance KDP",
            "to_unit_id": "Merch POD",
            "question": "capacity?",
        },
    )
    assert r.payload["status"] == "answered"
    assert r.payload["from_unit_id"] == str(tree["kdp"].id)
    assert r.payload["to_unit_id"] == str(tree["pod"].id)


@pytest.mark.asyncio
async def test_ask_about_auto_defaults_from_unit_id_to_ctx(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """When from_unit_id is omitted, the skill picks up ctx.business_unit_id."""
    skill = default_registry.skills["cooperation.ask_about"]
    r = await skill.run(
        ctx=_ctx(session, business, founder, unit_id=tree["kdp"].id),
        args={
            "to_unit_id": "Merch POD",
            "question": "do you have stock?",
        },
    )
    assert r.payload["status"] == "answered"
    assert r.payload["from_unit_id"] == str(tree["kdp"].id)
    # Audit log row exists
    logs = list(session.exec(select(CrossUnitQueryLog)).all())
    assert len(logs) == 1
    assert logs[0].from_unit_id == tree["kdp"].id


@pytest.mark.asyncio
async def test_ask_about_missing_from_and_no_ctx_errors(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """No from_unit_id arg + no ctx unit → helpful error, not a crash."""
    skill = default_registry.skills["cooperation.ask_about"]
    with pytest.raises(SkillError, match="from_unit_id not provided"):
        await skill.run(
            ctx=_ctx(session, business, founder, unit_id=None),
            args={
                "to_unit_id": "Merch POD",
                "question": "anyone home?",
            },
        )


@pytest.mark.asyncio
async def test_ask_about_unknown_to_unit_errors(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    skill = default_registry.skills["cooperation.ask_about"]
    with pytest.raises(SkillError, match="No BusinessUnit named"):
        await skill.run(
            ctx=_ctx(session, business, founder, unit_id=tree["kdp"].id),
            args={
                "to_unit_id": "Bogus Line",
                "question": "x",
            },
        )


# ----------------------------------------------------------------------------
# memory.recall with namespace_id as name
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_accepts_unit_name_for_namespace_id(
    session: Session, business: Business, founder: Founder,
    tree: dict[str, BusinessUnit],
) -> None:
    """memory.recall lets the caller pass a unit name to read from
    a sibling's namespace (requires a grant for cross-namespace)."""
    # Seed POD-namespace memory
    session.add(LongTermMemoryEntry(
        business_id=business.id, founder_id=founder.id,
        text="POD has free t-shirt capacity",
        tags=["capacity"],
        namespace_id=tree["pod"].memory_namespace_id,
    ))
    session.commit()
    # Grant KDP cross-namespace recall to POD's ns. Needs a parent
    # CooperationProposal — create + accept one to make the grant valid.
    from korpha.cooperation.model import (
        CooperationProposal, CooperationStatus,
    )
    from korpha.memory.grants import CrossNamespaceRecallGrant
    prop = CooperationProposal(
        business_id=business.id,
        from_unit_id=tree["kdp"].id, to_unit_id=tree["pod"].id,
        summary="recall grant test", status=CooperationStatus.ACCEPTED,
        permissions={"cross_namespace_recall": True},
    )
    session.add(prop)
    session.commit()
    session.refresh(prop)
    session.add(CrossNamespaceRecallGrant(
        from_namespace_id=tree["kdp"].memory_namespace_id,
        to_namespace_id=tree["pod"].memory_namespace_id,
        cooperation_proposal_id=prop.id,
    ))
    session.commit()

    recall = default_registry.skills["memory.recall"]
    r = await recall.run(
        ctx=_ctx(session, business, founder, unit_id=tree["kdp"].id),
        args={"query": "capacity", "namespace_id": "Merch POD"},
    )
    texts = " ".join(m["text"] for m in r.payload.get("results", []))
    assert "POD has free t-shirt capacity" in texts
