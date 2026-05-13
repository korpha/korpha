"""Tests for Stripe webhook ingestion + the new RevenueEvent table."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from korpha.business.model import Business
from korpha.commerce.revenue import (
    RevenueEvent, RevenueKind, RevenueService,
)
from korpha.commerce.stripe_webhook import (
    StripeWebhookError, process_webhook, verify_signature,
)
from korpha.identity.model import Founder


_SECRET = "whsec_test_super_secret"


def _sign(payload: bytes, secret: str = _SECRET, *, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={sig}"


def _checkout_event(business_id: str | None = None, *, amount: int = 2900) -> dict:
    obj: dict = {
        "id": "cs_test_abc",
        "amount_total": amount,
        "currency": "usd",
        "customer": "cus_test_123",
        "customer_details": {"email": "buyer@example.com"},
        "metadata": {},
    }
    if business_id is not None:
        obj["client_reference_id"] = business_id
    return {
        "id": "evt_test_1",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {"object": obj},
    }


# ---- verify_signature ----


def test_verify_signature_happy_path() -> None:
    payload = b'{"id":"evt_1"}'
    header = _sign(payload)
    ts = verify_signature(
        payload=payload, signature_header=header, secret=_SECRET,
    )
    assert isinstance(ts, int)


def test_verify_signature_rejects_missing_header() -> None:
    with pytest.raises(StripeWebhookError, match="missing"):
        verify_signature(
            payload=b"x", signature_header=None, secret=_SECRET,
        )


def test_verify_signature_rejects_no_secret() -> None:
    with pytest.raises(StripeWebhookError) as exc:
        verify_signature(
            payload=b"x", signature_header="t=1,v1=ab",
            secret="",
        )
    assert exc.value.status_code == 503


def test_verify_signature_rejects_wrong_signature() -> None:
    payload = b"hi"
    bad_header = "t=" + str(int(time.time())) + ",v1=deadbeef"
    with pytest.raises(StripeWebhookError, match="verification failed"):
        verify_signature(
            payload=payload, signature_header=bad_header,
            secret=_SECRET,
        )


def test_verify_signature_rejects_old_timestamp() -> None:
    payload = b"hi"
    # 10 minutes ago
    old_ts = int(time.time()) - 600
    header = _sign(payload, ts=old_ts)
    with pytest.raises(StripeWebhookError, match="replay"):
        verify_signature(
            payload=payload, signature_header=header, secret=_SECRET,
        )


def test_verify_signature_accepts_when_one_of_multiple_sigs_match() -> None:
    """During key rotation Stripe sends multiple v1 sigs."""
    payload = b'{"id":"evt"}'
    ts = int(time.time())
    correct_sig = hmac.new(
        _SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256,
    ).hexdigest()
    header = f"t={ts},v1=deadbeef,v1={correct_sig}"
    # Should pass — second sig is good
    verify_signature(
        payload=payload, signature_header=header, secret=_SECRET,
    )


def test_verify_signature_malformed_t() -> None:
    with pytest.raises(StripeWebhookError, match="bad t"):
        verify_signature(
            payload=b"x", signature_header="t=abc,v1=ff",
            secret=_SECRET,
        )


# ---- RevenueService ----


def test_record_is_idempotent_on_external_id(
    session: Session, business: Business,
) -> None:
    svc = RevenueService(session)
    now = datetime.now(tz=timezone.utc)
    a, created_a = svc.record(
        business_id=business.id, provider="stripe",
        external_id="evt_x", amount_usd=Decimal("29.00"),
        occurred_at=now,
    )
    b, created_b = svc.record(
        business_id=business.id, provider="stripe",
        external_id="evt_x", amount_usd=Decimal("29.00"),
        occurred_at=now,
    )
    assert created_a is True
    assert created_b is False
    assert a.id == b.id


def test_total_in_window_subtracts_refunds(
    session: Session, business: Business,
) -> None:
    svc = RevenueService(session)
    now = datetime.now(tz=timezone.utc)
    svc.record(
        business_id=business.id, provider="stripe",
        external_id="evt1", amount_usd=Decimal("100.00"),
        occurred_at=now - timedelta(days=2),
        kind=RevenueKind.ONE_TIME,
    )
    svc.record(
        business_id=business.id, provider="stripe",
        external_id="evt2", amount_usd=Decimal("30.00"),
        occurred_at=now - timedelta(days=1),
        kind=RevenueKind.REFUND,
    )
    total = svc.total_in_window(
        business_id=business.id,
        start=now - timedelta(days=7), end=now,
    )
    assert total == Decimal("70.00")


def test_total_excludes_other_business(
    session: Session, business: Business, founder: Founder,
) -> None:
    other = Business(
        founder_id=founder.id, name="Other", description="",
    )
    session.add(other); session.commit(); session.refresh(other)
    svc = RevenueService(session)
    now = datetime.now(tz=timezone.utc)
    svc.record(
        business_id=other.id, provider="stripe",
        external_id="theirs", amount_usd=Decimal("999.00"),
        occurred_at=now,
    )
    svc.record(
        business_id=business.id, provider="stripe",
        external_id="ours", amount_usd=Decimal("29.00"),
        occurred_at=now,
    )
    total = svc.total_in_window(
        business_id=business.id,
        start=now - timedelta(days=7), end=now + timedelta(days=1),
    )
    assert total == Decimal("29.00")


# ---- process_webhook ----


def test_process_webhook_persists_checkout(
    session: Session, business: Business,
) -> None:
    event = _checkout_event(business_id=str(business.id))
    payload = json.dumps(event).encode()
    header = _sign(payload)
    outcome = process_webhook(
        session=session, payload=payload,
        signature_header=header, secret=_SECRET,
    )
    assert outcome.status_code == 200
    assert outcome.event_kind == "checkout.session.completed"
    assert outcome.persisted is True

    rows = list(session.exec(select(RevenueEvent)).all())
    assert len(rows) == 1
    assert rows[0].amount_usd == Decimal("29.00")
    assert rows[0].kind == RevenueKind.ONE_TIME
    assert rows[0].customer_email == "buyer@example.com"


def test_process_webhook_handles_invoice_paid(
    session: Session, business: Business,
) -> None:
    event = {
        "id": "evt_inv_1",
        "type": "invoice.paid",
        "created": int(time.time()),
        "data": {"object": {
            "id": "in_test_1",
            "amount_paid": 4900,
            "customer": "cus_x",
            "client_reference_id": str(business.id),
            "metadata": {},
        }},
    }
    payload = json.dumps(event).encode()
    header = _sign(payload)
    outcome = process_webhook(
        session=session, payload=payload,
        signature_header=header, secret=_SECRET,
    )
    assert outcome.persisted is True
    rows = list(session.exec(select(RevenueEvent)).all())
    assert rows[0].kind == RevenueKind.SUBSCRIPTION
    assert rows[0].amount_usd == Decimal("49.00")


def test_process_webhook_handles_refund(
    session: Session, business: Business,
) -> None:
    event = {
        "id": "evt_refund_1",
        "type": "charge.refunded",
        "created": int(time.time()),
        "data": {"object": {
            "id": "ch_x",
            "amount_refunded": 1500,
            "client_reference_id": str(business.id),
            "metadata": {},
        }},
    }
    payload = json.dumps(event).encode()
    outcome = process_webhook(
        session=session, payload=payload,
        signature_header=_sign(payload), secret=_SECRET,
    )
    assert outcome.persisted is True
    rows = list(session.exec(select(RevenueEvent)).all())
    assert rows[0].kind == RevenueKind.REFUND
    assert rows[0].amount_usd == Decimal("15.00")


def test_process_webhook_duplicate_returns_persisted_false(
    session: Session, business: Business,
) -> None:
    event = _checkout_event(business_id=str(business.id))
    payload = json.dumps(event).encode()
    header = _sign(payload)
    process_webhook(
        session=session, payload=payload,
        signature_header=header, secret=_SECRET,
    )
    # Second call with the same event id (Stripe retry)
    outcome2 = process_webhook(
        session=session, payload=payload,
        signature_header=_sign(payload), secret=_SECRET,
    )
    assert outcome2.persisted is False
    assert outcome2.note == "duplicate"
    assert len(list(session.exec(select(RevenueEvent)).all())) == 1


def test_process_webhook_unknown_event_type_no_persist(
    session: Session, business: Business,
) -> None:
    event = {
        "id": "evt_meaningless",
        "type": "customer.updated",
        "created": int(time.time()),
        "data": {"object": {
            "client_reference_id": str(business.id),
        }},
    }
    payload = json.dumps(event).encode()
    outcome = process_webhook(
        session=session, payload=payload,
        signature_header=_sign(payload), secret=_SECRET,
    )
    assert outcome.status_code == 200
    assert outcome.persisted is False
    assert outcome.note == "event_type_not_tracked"
    assert list(session.exec(select(RevenueEvent)).all()) == []


def test_process_webhook_resolves_business_via_metadata(
    session: Session, business: Business,
) -> None:
    """metadata.business_id used when client_reference_id missing."""
    event = _checkout_event()  # no client_reference_id
    event["data"]["object"]["metadata"] = {
        "business_id": str(business.id),
    }
    payload = json.dumps(event).encode()
    outcome = process_webhook(
        session=session, payload=payload,
        signature_header=_sign(payload), secret=_SECRET,
    )
    assert outcome.persisted is True


def test_process_webhook_falls_back_to_single_business(
    session: Session, business: Business,
) -> None:
    """No ref id, no metadata, but only one Business in DB →
    attribute to it (single-tenant convenience)."""
    event = _checkout_event()  # no client_reference_id
    payload = json.dumps(event).encode()
    outcome = process_webhook(
        session=session, payload=payload,
        signature_header=_sign(payload), secret=_SECRET,
    )
    assert outcome.persisted is True


def test_process_webhook_drops_when_business_unresolvable(
    session: Session, business: Business, founder: Founder,
) -> None:
    """Multiple businesses + no ref id + no metadata → can't
    attribute, drop the event with status 200."""
    other = Business(
        founder_id=founder.id, name="Other", description="",
    )
    session.add(other); session.commit()

    event = _checkout_event()  # no ref / metadata
    payload = json.dumps(event).encode()
    outcome = process_webhook(
        session=session, payload=payload,
        signature_header=_sign(payload), secret=_SECRET,
    )
    assert outcome.status_code == 200
    assert outcome.persisted is False
    assert outcome.note == "business_not_resolved"


def test_process_webhook_bad_json_raises() -> None:
    with pytest.raises(StripeWebhookError, match="JSON"):
        process_webhook(
            session=None,  # type: ignore[arg-type]
            payload=b"\x00\xff\xff",
            signature_header=_sign(b"\x00\xff\xff"),
            secret=_SECRET,
        )


# ---- HTTP integration ----


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _SECRET)
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="t",
        )
        s.add(b); s.commit(); s.refresh(b)
    from korpha.api.server import build_app
    return TestClient(build_app()), tmp_path, b.id


def test_http_webhook_persists(http) -> None:
    client, _, biz_id = http
    event = _checkout_event(business_id=str(biz_id))
    payload = json.dumps(event).encode()
    r = client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"stripe-signature": _sign(payload)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["persisted"] is True


def test_http_webhook_rejects_bad_signature(http) -> None:
    client, _, biz_id = http
    event = _checkout_event(business_id=str(biz_id))
    payload = json.dumps(event).encode()
    bad_header = f"t={int(time.time())},v1=deadbeef"
    r = client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"stripe-signature": bad_header},
    )
    assert r.status_code == 400


def test_http_webhook_rejects_missing_signature(http) -> None:
    client, _, biz_id = http
    event = _checkout_event(business_id=str(biz_id))
    r = client.post(
        "/api/stripe/webhook",
        content=json.dumps(event).encode(),
    )
    assert r.status_code == 400


# ---- finance.monthly_review reads RevenueEvent ----


@pytest.mark.asyncio
async def test_monthly_review_uses_real_revenue_when_available(
    session: Session, business: Business, founder: Founder,
) -> None:
    """When RevenueEvent rows exist, monthly_review uses them
    instead of the legacy approval proxy."""
    from decimal import Decimal as _D
    from korpha.skills import default_registry
    from korpha.skills.types import SkillContext

    svc = RevenueService(session)
    now = datetime.now(tz=timezone.utc)
    svc.record(
        business_id=business.id, provider="stripe",
        external_id="evt_real_1", amount_usd=_D("99.00"),
        occurred_at=now - timedelta(days=3),
    )
    svc.record(
        business_id=business.id, provider="stripe",
        external_id="evt_real_2", amount_usd=_D("99.00"),
        occurred_at=now - timedelta(days=10),
    )

    # Stub LLM
    class _StubPool:
        async def complete(self, request, *, account=None):
            from korpha.inference.types import CompletionResponse
            return CompletionResponse(
                content=(
                    '{"headline":"two paying customers",'
                    '"trend":"improving",'
                    '"month_metrics":{"revenue_usd":198,"spend_usd":0,'
                    '"net_usd":198,"shipped_cards":0,'
                    '"spend_per_shipped":0},'
                    '"wins":["first revenue"],'
                    '"concerns":[],'
                    '"strategy_proposal":{"next_month_focus":"x",'
                    '"tasks":[],"kpi_target":"x"}}'
                ),
                tool_calls=(),
                input_tokens=10, output_tokens=200, cached_tokens=0,
                cost_usd=_D("0.001"),
                provider="mock", model="mock-pro", account_id="t",
                reasoning=None,
            )

    class _StubTracker:
        def __init__(self, pool):
            self.pool = pool
        async def complete(self, request, **_kw):
            return await self.pool.complete(request)

    pool = _StubPool()
    skill = default_registry.skills["finance.monthly_review"]
    ctx = SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=_StubTracker(pool),  # type: ignore[arg-type]
    )
    result = await skill.run(ctx=ctx, args={})
    assert result.payload["raw_inputs"]["revenue_usd"] == 198.0
