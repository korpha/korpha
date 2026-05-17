"""Tests for the research.x_search skill.

Covers:
  - Skill auto-registers only when xAI OAuth is configured
  - Input validation (query required, handle list ≤10)
  - xAI Responses API call shape (tool=x_search injected with params)
  - Response parsing (answer text + tweets out of the output array)
  - Auth-error surfaces as SkillError
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference import xai_oauth
from korpha.skills.types import SkillContext, SkillError


@pytest.fixture(autouse=True)
def temp_korpha_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    yield tmp_path


def _seed_xai_token():
    """Drop a working token in the vault so is_configured() is True."""
    xai_oauth._write_state(
        {
            "access_token": "tok_abc",
            "refresh_token": "ref_abc",
            "expires_at": int(time.time()) + 3600,
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        },
        business_unit_id=None,
    )


def _ctx() -> SkillContext:
    """Minimal SkillContext — most fields aren't read by x_search."""
    from sqlmodel import Session, SQLModel, create_engine
    import korpha.db.registry  # noqa: F401
    from korpha.business.model import Business
    from korpha.identity.model import Founder

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    session = Session(eng)
    f = Founder(email="m@x.com")
    session.add(f)
    session.commit()
    session.refresh(f)
    b = Business(founder_id=f.id, name="X")
    session.add(b)
    session.commit()
    session.refresh(b)
    return SkillContext(
        business=b,
        founder=f,
        session=session,
        cost_tracker=MagicMock(),
    )


# ---- registration --------------------------------------------------


def test_skill_not_registered_when_no_oauth():
    from korpha.skills import x_search
    from korpha.skills.registry import default_registry

    # Fresh registry: no auth → don't register.
    default_registry.skills.pop("research.x_search", None)
    x_search.register_skills()
    assert "research.x_search" not in default_registry.skills


def test_skill_registers_when_oauth_configured():
    from korpha.skills import x_search
    from korpha.skills.registry import default_registry

    _seed_xai_token()
    default_registry.skills.pop("research.x_search", None)
    x_search.register_skills()
    assert "research.x_search" in default_registry.skills


# ---- run() validation ----------------------------------------------


def test_run_rejects_missing_query():
    _seed_xai_token()
    from korpha.skills.x_search import XSearchSkill

    skill = XSearchSkill()
    import asyncio
    with pytest.raises(SkillError, match="query"):
        asyncio.run(skill.run(ctx=_ctx(), args={}))


def test_run_rejects_handle_list_too_long():
    _seed_xai_token()
    from korpha.skills.x_search import XSearchSkill

    skill = XSearchSkill()
    import asyncio
    with pytest.raises(SkillError, match="≤10"):
        asyncio.run(skill.run(
            ctx=_ctx(),
            args={
                "query": "test",
                "allowed_x_handles": [f"h{i}" for i in range(11)],
            },
        ))


def test_run_strips_at_prefix_from_handles(monkeypatch):
    _seed_xai_token()
    from korpha.skills.x_search import XSearchSkill

    seen_payloads: list[dict] = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                ],
                "usage": {"input_tokens": 50, "output_tokens": 20},
            }

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, headers=None):
            seen_payloads.append(json)
            return FakeResp()

    monkeypatch.setattr(
        "korpha.skills.x_search.httpx.AsyncClient", FakeClient,
    )

    import asyncio
    asyncio.run(XSearchSkill().run(
        ctx=_ctx(),
        args={
            "query": "what's happening",
            "allowed_x_handles": ["@xai", "@elonmusk", "  @nousresearch  "],
        },
    ))
    tool_cfg = seen_payloads[0]["tools"][0]
    assert tool_cfg["type"] == "x_search"
    assert tool_cfg["allowed_x_handles"] == [
        "xai", "elonmusk", "nousresearch",
    ]


# ---- response parsing ----------------------------------------------


def test_run_returns_answer_and_tweets(monkeypatch):
    _seed_xai_token()
    from korpha.skills.x_search import XSearchSkill

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "Several founders shipped this week.\n\n"
                                    "@indiehacker: shipped v2 ...\n"
                                    "@founderx: launched today ..."
                                ),
                            },
                        ],
                    },
                    {
                        "type": "x_search_results",
                        "results": [
                            {
                                "handle": "indiehacker",
                                "text": "shipped v2 today!",
                                "created_at": "2026-05-15",
                                "id": "tw_1",
                                "url": "https://x.com/indiehacker/status/tw_1",
                            },
                            {
                                "handle": "founderx",
                                "text": "launching the new pricing now",
                                "created_at": "2026-05-15",
                            },
                        ],
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 80},
            }

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, headers=None):
            return FakeResp()

    monkeypatch.setattr(
        "korpha.skills.x_search.httpx.AsyncClient", FakeClient,
    )

    import asyncio
    result = asyncio.run(XSearchSkill().run(
        ctx=_ctx(),
        args={"query": "indie hacker launches"},
    ))
    assert result.skill_name == "research.x_search"
    assert "founders shipped" in result.payload["answer"]
    assert len(result.payload["tweets"]) == 2
    assert result.payload["tweets"][0]["handle"] == "indiehacker"
    assert result.payload["input_tokens"] == 100
    assert result.cost_usd == 0.0


def test_run_surfaces_xai_4xx_as_skill_error(monkeypatch):
    _seed_xai_token()
    from korpha.skills.x_search import XSearchSkill

    class FakeResp:
        status_code = 401
        text = '{"error":"invalid_token"}'

        def json(self):
            return json.loads(self.text)

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, headers=None):
            return FakeResp()

    monkeypatch.setattr(
        "korpha.skills.x_search.httpx.AsyncClient", FakeClient,
    )

    import asyncio
    with pytest.raises(SkillError, match="401"):
        asyncio.run(XSearchSkill().run(
            ctx=_ctx(), args={"query": "x"},
        ))


def test_run_surfaces_no_auth_clean_message():
    """No xAI OAuth tokens → friendly SkillError instructing the
    founder where to fix it."""
    from korpha.skills.x_search import XSearchSkill

    skill = XSearchSkill()
    import asyncio
    with pytest.raises(SkillError, match="aigenteur auth add xai-oauth"):
        asyncio.run(skill.run(
            ctx=_ctx(), args={"query": "anything"},
        ))
