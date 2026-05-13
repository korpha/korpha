"""PR-INT-8 — full chat-driven E2E walkthrough of the org-mode stack.

Simulates a founder onboarding ("I want to do KDP and POD") and
verifies the entire integration layer reacts correctly:

* CEO is hired at business creation
* `hr.start_business_line` spawns each line + auto-hires a Line VP
* Each line gets its own memory_namespace_id (hard isolation)
* `memory.remember` and `memory.recall` are scoped per unit
* `cooperation.ask_about` dispatches synchronously across siblings
* Cross-tree queries are blocked without an explicit cooperation grant
* Per-unit credentials resolve via tree-walk fallback
* Kanban cards scoped to a unit are filterable
* Activity log captures the full journey

This is the "100% ready for running and testing" smoke that the
user asked for — not driven by a real LLM (no credentials in CI),
but exercising every wired component in the order a chat-driven
session would.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from sqlmodel import Session, select

from korpha.audit.model import Activity, InferenceTier
from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind,
)
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.cooperation.model import CrossUnitQueryLog
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.pool import InferencePool
from korpha.kanban import CreateCardInput, KanbanBoard
from korpha.kanban.model import KanbanCard, KanbanColumn
from korpha.memory.model import LongTermMemoryEntry
from korpha.skills import default_registry
from korpha.skills.types import SkillContext


def _ctx(session, business, founder, unit_id: UUID | None = None) -> SkillContext:
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
        business_unit_id=unit_id,
    )


def _bootstrap_default_unit(
    session: Session, business: Business,
) -> BusinessUnit:
    """Onboarding creates the DEFAULT unit. In real flow this happens
    via the migration backfill / onboarding chain. The test sets it
    up directly so we're testing org-mode plumbing, not the bootstrap."""
    board = BusinessUnitBoard(session)
    return board.create(
        business_id=business.id, name=business.name,
        kind=BusinessUnitKind.DEFAULT,
    )


@pytest.mark.asyncio
async def test_full_chat_driven_org_e2e(
    session: Session, business: Business, founder: Founder,
) -> None:
    """The headline test. Walks the entire stack."""

    # 1. Founder + Business already exist (via fixtures). Bootstrap
    #    the DEFAULT business unit + ensure a CEO.
    default_unit = _bootstrap_default_unit(session, business)
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)
    assert ceo.role_type == RoleType.CEO

    # 2. Founder tells CEO "I want to do KDP and POD" — CEO calls
    #    hr.start_business_line twice.
    start_skill = default_registry.skills["hr.start_business_line"]

    kdp_result = await start_skill.run(
        ctx=_ctx(session, business, founder, default_unit.id),
        args={"kind": "kdp", "name": "Romance KDP"},
    )
    assert kdp_result.payload["kind"] == "kdp"
    kdp_unit_id = UUID(kdp_result.payload["unit_id"])
    kdp_vp_id = UUID(kdp_result.payload["owner_agent_role_id"])

    pod_result = await start_skill.run(
        ctx=_ctx(session, business, founder, default_unit.id),
        args={"kind": "pod", "name": "Merch POD"},
    )
    pod_unit_id = UUID(pod_result.payload["unit_id"])
    pod_vp_id = UUID(pod_result.payload["owner_agent_role_id"])

    # 3. Assert each line is its own unit with its own namespace.
    kdp_unit = session.get(BusinessUnit, kdp_unit_id)
    pod_unit = session.get(BusinessUnit, pod_unit_id)
    assert kdp_unit is not None
    assert pod_unit is not None
    assert kdp_unit.kind == BusinessUnitKind.LINE
    assert pod_unit.kind == BusinessUnitKind.LINE
    assert kdp_unit.parent_id == default_unit.id
    assert pod_unit.parent_id == default_unit.id
    assert kdp_unit.memory_namespace_id != pod_unit.memory_namespace_id
    # Each line has an auto-hired VP wired to ownership.
    assert kdp_unit.owner_agent_role_id == kdp_vp_id
    assert pod_unit.owner_agent_role_id == pod_vp_id
    kdp_vp = session.get(AgentRole, kdp_vp_id)
    pod_vp = session.get(AgentRole, pod_vp_id)
    assert kdp_vp is not None and "KDP" in (kdp_vp.title or "").upper()
    assert pod_vp is not None and "POD" in (pod_vp.title or "").upper()

    # 4. KDP VP stores a fact in its namespace; POD VP stores a
    #    different fact in its namespace.
    remember = default_registry.skills["memory.remember"]
    await remember.run(
        ctx=_ctx(session, business, founder, kdp_unit_id),
        args={"text": "Highland Rogue series launches in 6 weeks",
              "tags": "kdp,launch"},
    )
    await remember.run(
        ctx=_ctx(session, business, founder, pod_unit_id),
        args={"text": "POD has free t-shirt capacity in May 2026",
              "tags": "pod,capacity"},
    )

    # Persisted with namespace ids that match their owning unit.
    entries = list(session.exec(select(LongTermMemoryEntry)).all())
    highland = next(
        (e for e in entries if "Highland Rogue" in e.text), None,
    )
    podcap = next(
        (e for e in entries if "POD has free" in e.text), None,
    )
    assert highland is not None, (
        f"Highland Rogue entry not persisted. Entries: "
        f"{[(e.text[:50], e.namespace_id) for e in entries]}"
    )
    assert podcap is not None
    assert highland.namespace_id == kdp_unit.memory_namespace_id
    assert podcap.namespace_id == pod_unit.memory_namespace_id

    # 5. memory.recall is scoped to the caller's namespace.
    recall = default_registry.skills["memory.recall"]
    kdp_recall = await recall.run(
        ctx=_ctx(session, business, founder, kdp_unit_id),
        args={"query": "launches", "limit": 5},
    )
    kdp_texts = " ".join(
        m["text"] for m in kdp_recall.payload.get("results", [])
    )
    assert "Highland Rogue" in kdp_texts
    assert "POD has free t-shirt" not in kdp_texts  # other namespace

    pod_recall = await recall.run(
        ctx=_ctx(session, business, founder, pod_unit_id),
        args={"query": "capacity", "limit": 5},
    )
    pod_texts = " ".join(
        m["text"] for m in pod_recall.payload.get("results", [])
    )
    assert "POD has free t-shirt" in pod_texts
    assert "Highland Rogue" not in pod_texts

    # 6. cooperation.ask_about — KDP asks POD a question (sibling
    #    relationship, no grant needed).
    ask = default_registry.skills["cooperation.ask_about"]
    answer = await ask.run(
        ctx=_ctx(session, business, founder, kdp_unit_id),
        args={
            "from_unit_id": str(kdp_unit_id),
            "to_unit_id": str(pod_unit_id),
            "question": "Got capacity for Highland Rogue merch?",
        },
    )
    assert answer.payload["status"] == "answered"
    # The dispatcher answered AND found POD's memory about capacity
    relevant = answer.payload["response"]["relevant_memories"]
    capacity_hit = any(
        "POD has free t-shirt" in m["text"] for m in relevant
    )
    assert capacity_hit, (
        "POD's namespace memory should appear in the response"
    )
    # Audit log row was created.
    coop_logs = list(session.exec(select(CrossUnitQueryLog)).all())
    assert len(coop_logs) == 1
    assert coop_logs[0].response_summary  # captured by dispatch

    # 7. Kanban card scoped to KDP unit; the KanbanBoard filtering by
    #    business_unit_id only surfaces that line's cards.
    board = KanbanBoard(session)
    kdp_card = board.create(CreateCardInput(
        business_id=business.id, title="Draft Highland Rogue 3 cover",
        created_by_founder_id=founder.id,
    ))
    kdp_card.business_unit_id = kdp_unit_id
    session.add(kdp_card); session.commit()

    pod_card = board.create(CreateCardInput(
        business_id=business.id, title="Source bulk-print partner",
        created_by_founder_id=founder.id,
    ))
    pod_card.business_unit_id = pod_unit_id
    session.add(pod_card); session.commit()

    all_cards = list(session.exec(
        select(KanbanCard).where(KanbanCard.business_id == business.id)
    ).all())
    kdp_only = [
        c for c in all_cards if c.business_unit_id == kdp_unit_id
    ]
    pod_only = [
        c for c in all_cards if c.business_unit_id == pod_unit_id
    ]
    assert len(kdp_only) == 1
    assert "Highland Rogue" in kdp_only[0].title
    assert len(pod_only) == 1
    assert "bulk-print" in pod_only[0].title

    # 8. Activity log captured the founder/CEO journey.
    events = {
        a.event_type
        for a in session.exec(
            select(Activity).where(Activity.business_id == business.id)
        ).all()
    }
    assert "agent.hired" in events  # CEO + 2 VPs at minimum
    # At least 3 hire events (CEO + 2 VPs).
    hires = list(session.exec(
        select(Activity).where(
            Activity.business_id == business.id,
            Activity.event_type == "agent.hired",
        )
    ).all())
    assert len(hires) >= 3


@pytest.mark.asyncio
async def test_cross_tree_ask_about_blocked_without_grant(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Two lines under the same DEFAULT root are siblings (allowed).
    But a grandchild of one line vs a grandchild of another is
    cross-tree and requires a CooperationProposal with
    cross_tree_query=True."""
    default_unit = _bootstrap_default_unit(session, business)
    HiringService(session).ensure_ceo(business.id)

    start = default_registry.skills["hr.start_business_line"]
    kdp = await start.run(
        ctx=_ctx(session, business, founder, default_unit.id),
        args={"kind": "kdp", "name": "KDP"},
    )
    pod = await start.run(
        ctx=_ctx(session, business, founder, default_unit.id),
        args={"kind": "pod", "name": "POD"},
    )
    kdp_id = UUID(kdp.payload["unit_id"])
    pod_id = UUID(pod.payload["unit_id"])

    # Spawn grandchildren under each line — they're now cross-tree.
    board = BusinessUnitBoard(session)
    kdp_grandchild = board.create(
        business_id=business.id, name="Romance Type",
        kind=BusinessUnitKind.TYPE, parent_id=kdp_id,
    )
    pod_grandchild = board.create(
        business_id=business.id, name="T-Shirt Type",
        kind=BusinessUnitKind.TYPE, parent_id=pod_id,
    )

    from korpha.skills.types import SkillError
    ask = default_registry.skills["cooperation.ask_about"]
    with pytest.raises(SkillError, match="cross-tree query"):
        await ask.run(
            ctx=_ctx(session, business, founder, kdp_grandchild.id),
            args={
                "from_unit_id": str(kdp_grandchild.id),
                "to_unit_id": str(pod_grandchild.id),
                "question": "Hey can you collab on a Highland Rogue tee?",
            },
        )


@pytest.mark.asyncio
async def test_credential_resolution_walks_unit_tree(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Resolver finds an account scoped to an ANCESTOR when the leaf
    has none — that's the whole point of the tree-walk."""
    from korpha.credentials.model import (
        ExternalServiceAccount, ExternalServiceKind,
    )
    from korpha.credentials.resolver import (
        NoCredentialsAvailable, resolve_credentials,
    )

    default_unit = _bootstrap_default_unit(session, business)
    start = default_registry.skills["hr.start_business_line"]
    kdp = await start.run(
        ctx=_ctx(session, business, founder, default_unit.id),
        args={"kind": "kdp", "name": "KDP"},
    )
    kdp_id = UUID(kdp.payload["unit_id"])

    # Set up the company-wide default Stripe account (parent of all
    # units) — KDP itself has nothing.
    session.add(ExternalServiceAccount(
        business_id=business.id, business_unit_id=None,
        service=ExternalServiceKind.STRIPE,
        label="Company Stripe", credentials_encrypted=b"x",
        is_active=True,
    ))
    session.commit()

    # Resolver finds the parent-level account when called for KDP.
    resolved = resolve_credentials(
        session, business_id=business.id,
        business_unit_id=kdp_id,
        service=ExternalServiceKind.STRIPE,
    )
    assert resolved is not None
    assert resolved.account.label == "Company Stripe"

    # Now scope a KDP-specific Stripe account — resolver prefers it.
    session.add(ExternalServiceAccount(
        business_id=business.id, business_unit_id=kdp_id,
        service=ExternalServiceKind.STRIPE,
        label="KDP-only Stripe", credentials_encrypted=b"x",
        is_active=True,
    ))
    session.commit()
    resolved2 = resolve_credentials(
        session, business_id=business.id,
        business_unit_id=kdp_id,
        service=ExternalServiceKind.STRIPE,
    )
    assert resolved2.account.label == "KDP-only Stripe"
