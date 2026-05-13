"""Live Ollama Cloud integration test.

Skipped automatically when OLLAMA_CLOUD_API_KEY is not set, so CI and offline
tests stay green. Run locally with:

    OLLAMA_CLOUD_API_KEY=... pytest tests/integration -m integration

or simply ``pytest tests/integration`` if the .env file is loaded by your
shell. Tests are kept small to avoid token spend even on a subscription.
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest
from dotenv import load_dotenv

from korpha.audit.model import InferenceTier
from korpha.inference import (
    CompletionRequest,
    InferencePool,
    Message,
    ProviderAccount,
    Role,
    ollama_cloud_provider,
)
from korpha.inference.registry import AuthType

load_dotenv()

pytestmark = pytest.mark.integration

OLLAMA_KEY = os.getenv("OLLAMA_CLOUD_API_KEY")
SKIP_REASON = "OLLAMA_CLOUD_API_KEY not set in environment"


def _account() -> ProviderAccount:
    assert OLLAMA_KEY is not None
    return ProviderAccount(
        provider_name="ollama-cloud",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "deepseek-v4-flash:cloud",
            InferenceTier.PRO: "deepseek-v4-pro:cloud",
        },
        api_key=OLLAMA_KEY,
        label="ollama-cloud-1",
    )


@pytest.mark.skipif(OLLAMA_KEY is None, reason=SKIP_REASON)
@pytest.mark.asyncio
async def test_ollama_cloud_workhorse_real_call() -> None:
    """deepseek-v4-flash:cloud — workhorse tier round-trip."""
    pool = InferencePool(providers=[ollama_cloud_provider()], accounts=[_account()])
    request = CompletionRequest(
        messages=[Message(role=Role.USER, content="Reply with exactly: hi")],
        tier=InferenceTier.WORKHORSE,
        session_key="integration-flash",
        max_tokens=200,
    )
    response = await pool.complete(request)

    assert response.input_tokens > 0
    assert response.output_tokens > 0
    assert response.provider == "ollama-cloud"
    assert response.model == "deepseek-v4-flash:cloud"
    # Reasoning models may have empty content if reasoning ate the budget; just
    # require either content OR reasoning to be present.
    assert response.content or response.reasoning


@pytest.mark.skipif(OLLAMA_KEY is None, reason=SKIP_REASON)
@pytest.mark.asyncio
async def test_ollama_cloud_pro_returns_reasoning() -> None:
    """deepseek-v4-pro:cloud is a thinking model — reasoning must populate."""
    pool = InferencePool(providers=[ollama_cloud_provider()], accounts=[_account()])
    request = CompletionRequest(
        messages=[
            Message(
                role=Role.USER,
                content="What's 23 * 17? Show your work briefly.",
            )
        ],
        tier=InferenceTier.PRO,
        session_key="integration-pro",
        max_tokens=400,
    )
    response = await pool.complete(request)

    assert response.input_tokens > 0
    assert response.reasoning is not None
    assert len(response.reasoning) > 0


@pytest.mark.skipif(OLLAMA_KEY is None, reason=SKIP_REASON)
@pytest.mark.asyncio
async def test_subscription_account_costs_zero() -> None:
    """No pricing configured = subscription model = cost should be 0."""
    pool = InferencePool(providers=[ollama_cloud_provider()], accounts=[_account()])
    request = CompletionRequest(
        messages=[Message(role=Role.USER, content="hi")],
        tier=InferenceTier.WORKHORSE,
        session_key="integration-cost",
        max_tokens=20,
    )
    response = await pool.complete(request)
    assert response.cost_usd == Decimal("0")


@pytest.mark.skipif(OLLAMA_KEY is None, reason=SKIP_REASON)
@pytest.mark.asyncio
async def test_session_affinity_holds_with_real_provider() -> None:
    """Two calls with same session_key route to same account, even on real network."""
    account = _account()
    pool = InferencePool(providers=[ollama_cloud_provider()], accounts=[account])
    base = CompletionRequest(
        messages=[Message(role=Role.USER, content="hi")],
        tier=InferenceTier.WORKHORSE,
        session_key="affinity-real",
        max_tokens=20,
    )
    r1 = await pool.complete(base)
    r2 = await pool.complete(base)
    assert r1.account_id == r2.account_id == str(account.id)
