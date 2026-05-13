"""Tests for CodexCLIProvider — subscription-auth inference via subprocess.

We monkeypatch ``shutil.which`` and ``asyncio.create_subprocess_exec`` so the
tests don't require Codex to be installed and don't burn ChatGPT-subscription
turns. Real-binary integration is covered separately by the live install
smoke test in PROGRESS — out of test scope here.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.provider import ProviderError
from korpha.inference.providers.codex_cli import (
    CodexCLIProvider,
    _flatten_messages,
    _parse_ndjson,
)
from korpha.inference.registry import AuthType, ProviderAccount
from korpha.inference.types import CompletionRequest, Message, Role


def _account(model: str = "gpt-5") -> ProviderAccount:
    return ProviderAccount(
        provider_name="codex-cli",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.PRO: model, InferenceTier.WORKHORSE: "gpt-5-mini"},
        api_key="subscription",
    )


def _request(
    *,
    tier: InferenceTier = InferenceTier.PRO,
    user: str = "Hello",
    system: str | None = None,
) -> CompletionRequest:
    msgs: list[Message] = []
    if system is not None:
        msgs.append(Message(role=Role.SYSTEM, content=system))
    msgs.append(Message(role=Role.USER, content=user))
    return CompletionRequest(messages=msgs, tier=tier, session_key="t")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_flatten_messages_combines_system_then_turns() -> None:
    msgs = [
        Message(role=Role.SYSTEM, content="You are helpful."),
        Message(role=Role.USER, content="Hi"),
        Message(role=Role.ASSISTANT, content="Hello!"),
        Message(role=Role.USER, content="What's 2+2?"),
    ]
    flat = _flatten_messages(msgs)
    assert "<system>" in flat
    assert "You are helpful." in flat
    assert "User: Hi" in flat
    assert "Assistant: Hello!" in flat
    assert "User: What's 2+2?" in flat
    # System block is first
    assert flat.index("<system>") < flat.index("User:")


def test_parse_ndjson_concatenates_agent_messages() -> None:
    body = "\n".join([
        '{"type":"thread.started","thread_id":"x"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"part one"}}',
        '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"part two"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":5,"cached_input_tokens":80}}',
    ])
    text, usage = _parse_ndjson(body)
    assert "part one" in text
    assert "part two" in text
    assert usage == {"input_tokens": 100, "output_tokens": 5, "cached_input_tokens": 80}


def test_parse_ndjson_ignores_non_agent_items() -> None:
    body = "\n".join([
        '{"type":"item.completed","item":{"type":"tool_call","name":"shell"}}',
        '{"type":"turn.completed","usage":{}}',
    ])
    text, usage = _parse_ndjson(body)
    assert text == ""
    assert usage == {}


def test_parse_ndjson_tolerates_garbage_lines() -> None:
    body = "stray log line\n" + json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "ok"},
    })
    text, _ = _parse_ndjson(body)
    assert text == "ok"


# ---------------------------------------------------------------------------
# complete() with mocked subprocess
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        # Pretend to consume stdin
        del input
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass


@pytest.mark.asyncio
async def test_complete_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocks codex's NDJSON output and asserts we surface the right text +
    token counts + zero cost (subscription-paid)."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )
    body = "\n".join([
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"4"}}',
        '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":1,"cached_input_tokens":10}}',
    ]).encode("utf-8")

    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **_kwargs: Any) -> _FakeProc:
        captured["argv"] = args
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = CodexCLIProvider()
    r = await p.complete(_request(user="What is 2+2?"), _account())
    assert r.content == "4"
    assert r.input_tokens == 12
    assert r.output_tokens == 1
    assert r.cached_tokens == 10
    assert r.cache_hit_ratio == pytest.approx(10 / 12)
    assert float(r.cost_usd) == 0.0
    # argv must include the model + sandbox flags
    argv = captured["argv"]
    assert "codex" in argv[0]
    assert "exec" in argv
    assert "--json" in argv
    assert "-m" in argv
    assert "gpt-5" in argv  # tier=PRO → model from account


@pytest.mark.asyncio
async def test_complete_omits_model_flag_for_subscription_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the configured model is the ``codex-default`` sentinel we
    must NOT pass -m: Codex with a ChatGPT account rejects most explicit
    model names and picks based on subscription entitlement when given
    no override."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )
    body = b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n{"type":"turn.completed","usage":{}}'
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **_kwargs: Any) -> _FakeProc:
        captured["argv"] = args
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    account = ProviderAccount(
        provider_name="codex-cli",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.PRO: "codex-default"},
        api_key="subscription",
    )
    p = CodexCLIProvider()
    await p.complete(_request(), account)
    argv = captured["argv"]
    assert "-m" not in argv  # sentinel should suppress the flag


@pytest.mark.asyncio
async def test_complete_tolerates_nonzero_exit_after_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex sometimes exits 1 with a cosmetic 'failed to record rollout
    items' error AFTER emitting a complete response. We want to keep
    the response we already paid tokens for."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )
    body = b'{"type":"item.completed","item":{"type":"agent_message","text":"answered"}}\n{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":1,"cached_input_tokens":0}}'

    async def fake_exec(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(
            stdout=body,
            stderr=b"ERROR codex_core::session: failed to record rollout items",
            returncode=1,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = CodexCLIProvider()
    r = await p.complete(_request(), _account())
    assert r.content == "answered"
    assert r.output_tokens == 1


@pytest.mark.asyncio
async def test_complete_raises_when_codex_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    p = CodexCLIProvider()
    with pytest.raises(ProviderError, match=r"not on PATH"):
        await p.complete(_request(), _account())


@pytest.mark.asyncio
async def test_complete_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )

    async def fake_exec(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(
            stdout=b"", stderr=b"oauth token expired", returncode=2
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = CodexCLIProvider()
    with pytest.raises(ProviderError, match=r"exited 2"):
        await p.complete(_request(), _account())


@pytest.mark.asyncio
async def test_complete_raises_on_no_agent_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """If codex emitted no agent_message events we have no completion to
    return — the caller needs to know."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )
    body = b'{"type":"thread.started","thread_id":"x"}\n{"type":"turn.completed","usage":{}}\n'

    async def fake_exec(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = CodexCLIProvider()
    with pytest.raises(ProviderError, match=r"no agent_message"):
        await p.complete(_request(), _account())


@pytest.mark.asyncio
async def test_complete_raises_when_account_missing_model_for_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Account configured only for PRO; request a tier with no model
    mapped → clear error rather than silent failure."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )
    account = ProviderAccount(
        provider_name="codex-cli",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.PRO: "gpt-5"},  # workhorse missing
        api_key="subscription",
    )
    p = CodexCLIProvider()
    with pytest.raises(ProviderError, match=r"no model mapped for tier"):
        await p.complete(_request(tier=InferenceTier.WORKHORSE), account)
