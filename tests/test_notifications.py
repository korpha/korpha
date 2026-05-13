"""Tests for notifiers + digest renderer."""
from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session

from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
)
from korpha.audit.model import Cost, InferenceTier
from korpha.blockers.model import (
    Blocker,
    BlockerKind,
    BlockerStatus,
    BlockerUrgency,
)
from korpha.business.model import Business, Task, TaskStatus
from korpha.cofounder.hiring import HiringService
from korpha.identity.model import Founder
from korpha.notifications import (
    MockEmailNotifier,
    Notification,
    NotifierError,
    ResendEmailNotifier,
)
from korpha.notifications.digest import build_snapshot, render_digest

# ───────────────────────── notifier transport ─────────────────────────


@pytest.mark.asyncio
async def test_mock_records_sends() -> None:
    n = MockEmailNotifier()
    await n.send(Notification(to="a@b", subject="hi", text_body="x"))
    assert len(n.sent) == 1
    assert n.sent[0].subject == "hi"


@pytest.mark.asyncio
async def test_mock_can_raise() -> None:
    n = MockEmailNotifier(raise_with="quota exceeded")
    with pytest.raises(NotifierError) as exc:
        await n.send(Notification(to="a@b", subject="x", text_body="y"))
    assert "quota" in str(exc.value)


@pytest.mark.asyncio
async def test_resend_sends_well_formed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        captured.append(_j.loads(req.content.decode("utf-8")))
        assert req.headers["Authorization"] == "Bearer re_test"
        return httpx.Response(200, json={"id": "msg_123"})

    n = ResendEmailNotifier(default_from="bot@example.com")
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await n.send(
        Notification(
            to="mike@example.com",
            subject="hi",
            text_body="plain",
            html_body="<p>rich</p>",
        )
    )
    await n.close()
    assert captured[0]["from"] == "bot@example.com"
    assert captured[0]["to"] == ["mike@example.com"]
    assert captured[0]["subject"] == "hi"
    assert captured[0]["text"] == "plain"
    assert captured[0]["html"] == "<p>rich</p>"


@pytest.mark.asyncio
async def test_resend_raises_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"message": "from-domain not verified"}
        )

    n = ResendEmailNotifier(default_from="bot@example.com")
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(NotifierError) as exc:
        await n.send(Notification(to="x@y", subject="s", text_body="t"))
    assert "not verified" in str(exc.value)
    await n.close()


@pytest.mark.asyncio
async def test_resend_missing_api_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    n = ResendEmailNotifier(default_from="bot@example.com")
    with pytest.raises(NotifierError) as exc:
        await n.send(Notification(to="x@y", subject="s", text_body="t"))
    assert "RESEND_API_KEY" in str(exc.value)


@pytest.mark.asyncio
async def test_resend_missing_from_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("RESEND_FROM", raising=False)
    n = ResendEmailNotifier()
    with pytest.raises(NotifierError) as exc:
        await n.send(Notification(to="x@y", subject="s", text_body="t"))
    assert "from-address" in str(exc.value)


@pytest.mark.asyncio
async def test_notification_from_address_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j
        captured.append(_j.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"id": "msg"})

    n = ResendEmailNotifier(default_from="bot@example.com")
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await n.send(
        Notification(
            to="x@y",
            subject="s",
            text_body="t",
            from_address="vip@example.com",
        )
    )
    await n.close()
    assert captured[0]["from"] == "vip@example.com"


# ───────────────────────── digest builder ─────────────────────────


def test_snapshot_counts_match_db(
    session: Session, business: Business, founder: Founder
) -> None:
    hiring = HiringService(session)
    ceo = hiring.ensure_ceo(business.id)

    # Two pending + one approved approval.
    for i in range(2):
        session.add(
            Approval(
                business_id=business.id,
                agent_role_id=ceo.id,
                action_class=ActionClass.PUBLIC_POST,
                proposal_summary=f"approval {i}",
                action_payload={},
                status=ApprovalStatus.PENDING,
            )
        )
    session.add(
        Approval(
            business_id=business.id,
            agent_role_id=ceo.id,
            action_class=ActionClass.SPEND,
            proposal_summary="approved",
            action_payload={},
            status=ApprovalStatus.APPROVED,
        )
    )
    # One open blocker, one resolved.
    session.add(
        Blocker(
            business_id=business.id,
            requesting_agent_role_id=ceo.id,
            kind=BlockerKind.DECISION,
            urgency=BlockerUrgency.NORMAL,
            title="pick brand color",
            detail="hex?",
            status=BlockerStatus.OPEN,
        )
    )
    session.add(
        Blocker(
            business_id=business.id,
            requesting_agent_role_id=ceo.id,
            kind=BlockerKind.OTHER,
            title="resolved one",
            detail="",
            status=BlockerStatus.RESOLVED,
        )
    )
    # In-progress + done tasks.
    session.add(
        Task(
            business_id=business.id,
            ref_number=1,
            title="ship landing",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    session.add(
        Task(
            business_id=business.id,
            ref_number=2,
            title="set up stripe",
            status=TaskStatus.DONE,
        )
    )
    session.commit()

    snap = build_snapshot(session, business)
    assert snap.business_name == business.name
    assert snap.pending_approvals == 2
    assert snap.open_blockers == 1
    assert snap.in_progress_tasks == 1
    assert "pick brand color" in snap.open_blocker_titles
    assert any("approval" in s for s in snap.pending_approval_summaries)
    # Both tasks show in recent_task_lines (sorted by updated_at desc).
    assert len(snap.recent_task_lines) == 2


def test_snapshot_savings_uses_sonnet_baseline(
    session: Session, business: Business
) -> None:
    """50k input + 50k output tokens at our cost vs Sonnet should yield
    a positive savings number."""
    from decimal import Decimal

    session.add(
        Cost(
            business_id=business.id,
            provider="ollama-cloud",
            model="deepseek-v4-pro",
            tier=InferenceTier.PRO,
            input_tokens=50_000,
            output_tokens=50_000,
            cost_usd=Decimal("0.10"),  # cheap baseline
        )
    )
    session.commit()
    snap = build_snapshot(session, business)
    # Sonnet would cost: 0.05 * 3 + 0.05 * 15 = 0.15 + 0.75 = 0.90
    # We charged 0.10 → saved ~0.80
    assert snap.saved_vs_sonnet_usd > 0.5


def test_render_digest_contains_key_blocks(
    session: Session, business: Business
) -> None:
    snap = build_snapshot(session, business)
    notif = render_digest(snap, founder_name="Mike")
    assert "Mike" in notif.text_body
    assert business.name in notif.subject
    assert notif.html_body is not None
    assert "<html>" in notif.html_body.lower()
    assert business.name in notif.html_body


def test_render_digest_no_founder_name() -> None:
    snap = build_snapshot.__wrapped__ if hasattr(build_snapshot, "__wrapped__") else None
    snap = type(
        "S",
        (),
        {
            "business_name": "X",
            "pending_approvals": 0,
            "open_blockers": 0,
            "in_progress_tasks": 0,
            "today_spend_usd": 0.0,
            "saved_vs_sonnet_usd": 0.0,
            "open_blocker_titles": [],
            "pending_approval_summaries": [],
            "recent_task_lines": [],
        },
    )()
    notif = render_digest(snap)
    # Falls back to a generic salutation.
    assert "Morning," in notif.text_body
