"""Tests for ClaudeCodeProvider — subscription-auth inference via subprocess.

Mirror of test_codex_cli_provider.py. Subprocess + claude binary are mocked so
the test doesn't require a real Claude Pro / Max subscription on the box.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.provider import ProviderError
from korpha.inference.providers.claude_code import ClaudeCodeProvider
from korpha.inference.registry import AuthType, ProviderAccount
from korpha.inference.types import CompletionRequest, Message, Role


def _account(model: str = "sonnet") -> ProviderAccount:
    return ProviderAccount(
        provider_name="claude-code-cli",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.PRO: model, InferenceTier.WORKHORSE: "haiku"},
        api_key="subscription",
    )


def _request(
    *,
    tier: InferenceTier = InferenceTier.PRO,
    user: str = "Hello",
) -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role=Role.USER, content=user)],
        tier=tier,
        session_key="t",
    )


class _FakeProc:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        del input
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass


def _success_payload(
    *,
    text: str = "4",
    input_tokens: int = 2,
    cache_read: int = 11631,
    cache_creation: int = 5833,
    output: int = 5,
    cost_usd: float = 0.0,
) -> bytes:
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "stop_reason": "end_turn",
        "total_cost_usd": cost_usd,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output,
        },
    }).encode("utf-8")


@pytest.mark.asyncio
async def test_complete_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocks claude's JSON output and asserts we surface text + tokens
    + cache stats correctly. Claude splits inputs across uncached /
    cache_creation / cache_read; we sum those for input_tokens and
    surface cache_read as cached_tokens."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **_kwargs: Any) -> _FakeProc:
        captured["argv"] = args
        return _FakeProc(stdout=_success_payload())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = ClaudeCodeProvider()
    r = await p.complete(_request(user="What is 2+2?"), _account())
    assert r.content == "4"
    # 2 (uncached) + 5833 (cache creation) + 11631 (cache read)
    assert r.input_tokens == 17466
    assert r.output_tokens == 5
    assert r.cached_tokens == 11631
    assert r.cache_hit_ratio == pytest.approx(11631 / 17466)
    assert float(r.cost_usd) == 0.0
    # argv shape
    argv = captured["argv"]
    assert argv[0].endswith("claude")
    assert "--print" in argv
    assert "--output-format" in argv
    assert "json" in argv
    assert "--model" in argv
    assert "sonnet" in argv


@pytest.mark.asyncio
async def test_complete_omits_model_for_default_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model name is the ``claude-default`` sentinel we drop
    --model so claude picks based on subscription."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **_kwargs: Any) -> _FakeProc:
        captured["argv"] = args
        return _FakeProc(stdout=_success_payload())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    account = ProviderAccount(
        provider_name="claude-code-cli",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.PRO: "claude-default"},
        api_key="subscription",
    )
    p = ClaudeCodeProvider()
    await p.complete(_request(), account)
    assert "--model" not in captured["argv"]


@pytest.mark.asyncio
async def test_complete_raises_when_claude_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    p = ClaudeCodeProvider()
    with pytest.raises(ProviderError, match=r"not on PATH"):
        await p.complete(_request(), _account())


@pytest.mark.asyncio
async def test_complete_raises_on_is_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude returns is_error=True when not logged in or rate-limited.
    Surface the result string as the error message."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )
    body = json.dumps({
        "type": "result",
        "is_error": True,
        "result": "Not logged in · Please run /login",
        "usage": {},
    }).encode("utf-8")

    async def fake_exec(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = ClaudeCodeProvider()
    with pytest.raises(ProviderError, match=r"Not logged in"):
        await p.complete(_request(), _account())


@pytest.mark.asyncio
async def test_complete_raises_on_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )

    async def fake_exec(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(stdout=b"hello, world (not json)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = ClaudeCodeProvider()
    with pytest.raises(ProviderError, match=r"non-JSON"):
        await p.complete(_request(), _account())


@pytest.mark.asyncio
async def test_complete_passes_through_real_cost_when_api_key_billed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscription users see total_cost_usd=0; API-key-billed users
    see real $. Either way we must surface what the CLI reports."""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
    )

    async def fake_exec(*_args: str, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(stdout=_success_payload(cost_usd=0.0258))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = ClaudeCodeProvider()
    r = await p.complete(_request(), _account())
    assert float(r.cost_usd) == pytest.approx(0.0258)
