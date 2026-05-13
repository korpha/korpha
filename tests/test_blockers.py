"""BlockerQueue + ChiefOfStaff tests."""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from korpha.approvals.gate import ApprovalGate
from korpha.approvals.model import AutonomyMode
from korpha.audit.model import Activity
from korpha.blockers import (
    Blocker,
    BlockerKind,
    BlockerQueue,
    BlockerStatus,
    BlockerSubmission,
    BlockerUrgency,
)
from korpha.business.model import Business
from korpha.cofounder.chief_of_staff import ChiefOfStaff
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType


def _build_cos(session: Session) -> ChiefOfStaff:
    return ChiefOfStaff(
        session=session,
        queue=BlockerQueue(session=session),
        hiring=HiringService(session),
        gate=ApprovalGate(session),
    )


def _submit(
    queue: BlockerQueue,
    business: Business,
    cmo: AgentRole,
    title: str,
    *,
    kind: BlockerKind = BlockerKind.DECISION,
    urgency: BlockerUrgency = BlockerUrgency.NORMAL,
    options: list[str] | None = None,
    detail: str = "",
) -> Blocker:
    return queue.submit(
        BlockerSubmission(
            business_id=business.id,
            requesting_agent_role_id=cmo.id,
            title=title,
            kind=kind,
            urgency=urgency,
            options=options or [],
            detail=detail,
        )
    )


def test_submit_persists_blocker(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    queue = BlockerQueue(session=session)
    blocker = _submit(queue, business, cmo, "Need budget for ads")
    assert blocker.status == BlockerStatus.OPEN
    assert blocker.title == "Need budget for ads"
    assert blocker.requesting_agent_role_id == cmo.id


def test_dedupe_same_title_in_window(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    queue = BlockerQueue(session=session)
    a = _submit(queue, business, cmo, "Need budget for ads")
    b = _submit(queue, business, cmo, "  Need Budget For Ads  ")  # case + space
    assert a.id != b.id
    assert b.deduped_into_id == a.id
    assert b.status == BlockerStatus.DROPPED


def test_dedupe_bumps_urgency_on_canonical(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    queue = BlockerQueue(session=session)
    a = _submit(queue, business, cmo, "Need budget", urgency=BlockerUrgency.LOW)
    _submit(queue, business, cmo, "Need budget", urgency=BlockerUrgency.URGENT)
    session.refresh(a)
    assert a.urgency == BlockerUrgency.URGENT


def test_resolved_blocker_does_not_dedupe(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    queue = BlockerQueue(session=session)
    a = _submit(queue, business, cmo, "Topic X")
    queue.mark_resolved(a.id, resolution="done")
    b = _submit(queue, business, cmo, "Topic X")
    assert b.id != a.id
    assert b.deduped_into_id is None


def test_list_open_excludes_dupes(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    queue = BlockerQueue(session=session)
    _submit(queue, business, cmo, "X")
    _submit(queue, business, cmo, "X")  # dupe
    _submit(queue, business, cmo, "Y")
    open_list = queue.list_open(business.id)
    assert len(open_list) == 2
    assert {b.title for b in open_list} == {"X", "Y"}


def test_cos_triage_marks_awaiting_founder(
    session: Session, business: Business, cmo: AgentRole, ceo: AgentRole
) -> None:
    """ceo fixture is needed only to ensure CoS doesn't try to be the same role."""
    cos = _build_cos(session)
    queue = BlockerQueue(session=session)
    _submit(queue, business, cmo, "Need budget", options=["$500/mo", "$1000/mo"])

    digest = cos.digest_for_ceo(business.id)
    assert digest.total_open == 1
    assert digest.items[0].cos_recommendation is not None


def test_cos_topic_grouping(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    cos = _build_cos(session)
    queue = BlockerQueue(session=session)
    _submit(queue, business, cmo, "Twitter post copy approval", detail="Tweet draft")
    _submit(queue, business, cmo, "LinkedIn post timing", detail="When to post")
    _submit(queue, business, cmo, "Need ad budget", detail="$500 for ads")

    digest = cos.digest_for_ceo(business.id)
    assert "social" in digest.grouped_by_topic
    assert "spend" in digest.grouped_by_topic


def test_cos_urgency_orders_digest(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    cos = _build_cos(session)
    queue = BlockerQueue(session=session)
    _submit(queue, business, cmo, "low thing", urgency=BlockerUrgency.LOW)
    _submit(queue, business, cmo, "urgent thing", urgency=BlockerUrgency.URGENT)
    _submit(queue, business, cmo, "high thing", urgency=BlockerUrgency.HIGH)

    digest = cos.digest_for_ceo(business.id)
    titles = [item.title for item in digest.items]
    assert titles[0] == "urgent thing"
    assert titles[1] == "high thing"
    assert titles[2] == "low thing"


def test_cos_max_digest_items(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    cos = ChiefOfStaff(
        session=session,
        queue=BlockerQueue(session=session),
        hiring=HiringService(session),
        gate=ApprovalGate(session),
        max_digest_items=2,
    )
    queue = BlockerQueue(session=session)
    for i in range(5):
        _submit(queue, business, cmo, f"item {i}")
    digest = cos.digest_for_ceo(business.id)
    assert len(digest.items) == 2
    assert digest.total_open == 5


def test_cos_resolves_permission_within_auto_envelope(
    session: Session,
    business: Business,
    founder: object,
    cmo: AgentRole,
) -> None:
    """If the action class envelope is AUTO, CoS resolves PERMISSION blockers."""
    from korpha.approvals.model import ActionClass
    from korpha.identity.model import Founder as FounderModel

    assert isinstance(founder, FounderModel)
    gate = ApprovalGate(session)
    gate.set_mode(
        business_id=business.id,
        action_class=ActionClass.INTERNAL,
        platform=None,
        mode=AutonomyMode.AUTO,
        actor_id=founder.id,
    )

    cos = _build_cos(session)
    queue = BlockerQueue(session=session)
    blocker = _submit(
        queue,
        business,
        cmo,
        "permission to draft post",
        kind=BlockerKind.PERMISSION,
        options=["draft and send"],
    )
    cos.digest_for_ceo(business.id)
    session.refresh(blocker)
    assert blocker.status == BlockerStatus.RESOLVED_BY_COS
    assert blocker.resolution is not None


def test_cos_role_auto_hired_with_ceo(session: Session, business: Business) -> None:
    """ensure_ceo also hires CoS — CoS is not user-facing but always present."""
    hiring = HiringService(session)
    hiring.ensure_ceo(business.id)
    cos = hiring.get_active_role(business.id, RoleType.CHIEF_OF_STAFF)
    assert cos is not None
    assert cos.title == "Chief of Staff"


def test_dropped_blocker_emits_activity(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    queue = BlockerQueue(session=session)
    _submit(queue, business, cmo, "X")
    _submit(queue, business, cmo, "X")
    activities = session.exec(
        select(Activity).where(Activity.event_type == "blocker.duplicate")
    ).all()
    assert len(activities) == 1


def test_digest_render_includes_items(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    cos = _build_cos(session)
    queue = BlockerQueue(session=session)
    _submit(
        queue,
        business,
        cmo,
        "Pick a niche",
        kind=BlockerKind.DECISION,
        urgency=BlockerUrgency.HIGH,
        options=["B2B SaaS", "Consumer app"],
        detail="Need direction before committing 3 weeks of work",
    )
    digest = cos.digest_for_ceo(business.id)
    rendered = digest.render()
    assert "Pick a niche" in rendered
    assert "B2B SaaS" in rendered
    assert "HIGH" in rendered


def test_digest_headline_when_empty(session: Session, business: Business) -> None:
    cos = _build_cos(session)
    digest = cos.digest_for_ceo(business.id)
    assert "No blockers" in digest.headline()


# ---- LLM-driven triage ----


@pytest.mark.asyncio
async def test_llm_triage_overrides_recommendations(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    """When use_llm_triage=True and cost_tracker provided, LLM-crafted
    recommendations replace the rule-based defaults."""
    from korpha.audit.model import InferenceTier
    from korpha.inference import (
        InferencePool,
        MockProvider,
        ProviderAccount,
    )
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.registry import AuthType

    response = (
        '{"items":['
        '{"id":"<TBD>","rank":1,'
        '"recommendation":"Pick option 2 — it bypasses the dependency on the design call",'
        '"note":"This unblocks the launch by Friday."}'
        ']}'
    )
    pool = InferencePool(
        providers=[MockProvider(static_response=response)],
        accounts=[
            ProviderAccount(
                provider_name="mock",
                auth_type=AuthType.API_KEY,
                tier_models={
                    InferenceTier.WORKHORSE: "mock-flash",
                    InferenceTier.PRO: "mock-pro",
                },
                api_key="x",
            )
        ],
    )
    tracker = CostTracker(pool=pool)

    queue = BlockerQueue(session=session)
    blocker = _submit(
        queue, business, cmo, "Pick deploy host", options=["Vercel", "Fly", "VPS"]
    )

    # Patch the static response to embed the actual blocker id.
    real_response = response.replace("<TBD>", str(blocker.id))
    pool.providers[0].static_response = real_response  # type: ignore[attr-defined]

    cos = ChiefOfStaff(
        session=session,
        queue=queue,
        hiring=HiringService(session),
        gate=ApprovalGate(session),
        cost_tracker=tracker,
        use_llm_triage=True,
    )

    digest = await cos.digest_for_ceo_async(business.id)
    assert digest.items, "expected one digest item"
    rec = digest.items[0].cos_recommendation or ""
    assert "Pick option 2" in rec or "bypasses" in rec


@pytest.mark.asyncio
async def test_llm_triage_falls_back_on_parse_failure(
    session: Session, business: Business, cmo: AgentRole
) -> None:
    """LLM returns garbage → keep the rule-based recommendation."""
    from korpha.audit.model import InferenceTier
    from korpha.inference import (
        InferencePool,
        MockProvider,
        ProviderAccount,
    )
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.registry import AuthType

    pool = InferencePool(
        providers=[MockProvider(static_response="not json at all")],
        accounts=[
            ProviderAccount(
                provider_name="mock",
                auth_type=AuthType.API_KEY,
                tier_models={
                    InferenceTier.WORKHORSE: "mock-flash",
                    InferenceTier.PRO: "mock-pro",
                },
                api_key="x",
            )
        ],
    )
    tracker = CostTracker(pool=pool)
    queue = BlockerQueue(session=session)
    _submit(queue, business, cmo, "Need budget", options=["$300", "$500"])

    cos = ChiefOfStaff(
        session=session,
        queue=queue,
        hiring=HiringService(session),
        gate=ApprovalGate(session),
        cost_tracker=tracker,
        use_llm_triage=True,
    )
    digest = await cos.digest_for_ceo_async(business.id)
    assert digest.items
    # Falls back to rule-based "Go with: <first option>" recommendation.
    rec = digest.items[0].cos_recommendation or ""
    assert "Go with" in rec
