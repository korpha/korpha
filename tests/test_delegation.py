"""Delegation CLI wrapper tests using subprocess fakes.

We mock asyncio.create_subprocess_exec so tests run offline and never spend
real Claude / Codex tokens.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import patch

import pytest

from korpha.delegation import (
    ClaudeCodeCLI,
    CodexCLI,
    DelegationBudgetExceeded,
    DelegationError,
    DelegationRequest,
)


@dataclass
class _FakeProc:
    stdout: bytes
    stderr: bytes
    returncode: int = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        pass


def _patch_subprocess(stdout: str, *, stderr: str = "", returncode: int = 0) -> object:
    fake = _FakeProc(
        stdout=stdout.encode("utf-8"),
        stderr=stderr.encode("utf-8"),
        returncode=returncode,
    )

    async def fake_create(*args: object, **kwargs: object) -> _FakeProc:
        return fake

    return patch("asyncio.create_subprocess_exec", side_effect=fake_create)


def _claude_success_payload() -> dict[str, object]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 543,
        "result": "Mike, the niche is B2B onboarding for indie SaaS.",
        "session_id": "abc-123",
        "total_cost_usd": 0.0042,
        "usage": {
            "input_tokens": 1500,
            "output_tokens": 220,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0,
        },
    }


@pytest.mark.asyncio
async def test_claude_code_parses_success() -> None:
    payload = json.dumps(_claude_success_payload())
    cli = ClaudeCodeCLI()
    request = DelegationRequest(prompt="Pick a niche", max_budget_usd=Decimal("0.05"))

    with _patch_subprocess(payload):
        response = await cli.run(request)

    assert response.is_error is False
    assert "B2B onboarding" in response.content
    assert response.cost_usd == Decimal("0.0042")
    assert response.input_tokens == 1500
    assert response.output_tokens == 220
    assert response.cached_tokens == 800
    assert response.session_id == "abc-123"


@pytest.mark.asyncio
async def test_claude_code_requires_budget() -> None:
    cli = ClaudeCodeCLI()
    with pytest.raises(DelegationError):
        await cli.run(DelegationRequest(prompt="hi"))


@pytest.mark.asyncio
async def test_claude_code_budget_exceeded_raises() -> None:
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "duration_ms": 1000,
            "total_cost_usd": 0.052,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "errors": ["Reached maximum budget ($0.05)"],
        }
    )
    cli = ClaudeCodeCLI()
    request = DelegationRequest(prompt="hi", max_budget_usd=Decimal("0.05"))

    with _patch_subprocess(payload), pytest.raises(DelegationBudgetExceeded) as exc_info:
        await cli.run(request)
    assert exc_info.value.cost_usd == Decimal("0.052")


@pytest.mark.asyncio
async def test_claude_code_handles_prelude_then_json() -> None:
    """Some claude versions print log lines before the JSON object."""
    payload = "Some preamble line\nAnother line\n" + json.dumps(_claude_success_payload())
    cli = ClaudeCodeCLI()
    request = DelegationRequest(prompt="hi", max_budget_usd=Decimal("0.05"))

    with _patch_subprocess(payload):
        response = await cli.run(request)
    assert "B2B onboarding" in response.content


@pytest.mark.asyncio
async def test_claude_code_non_json_stdout_raises() -> None:
    cli = ClaudeCodeCLI()
    request = DelegationRequest(prompt="hi", max_budget_usd=Decimal("0.05"))
    with _patch_subprocess("This is not JSON output"), pytest.raises(DelegationError):
        await cli.run(request)


@pytest.mark.asyncio
async def test_codex_returns_stdout_as_content() -> None:
    output = "Generated landing page in /tmp/proj. Files: index.html, style.css.\n"
    cli = CodexCLI()
    request = DelegationRequest(prompt="Build a landing page")

    with _patch_subprocess(output):
        response = await cli.run(request)

    assert response.is_error is False
    assert "Generated landing page" in response.content


@pytest.mark.asyncio
async def test_codex_non_zero_exit_raises() -> None:
    cli = CodexCLI()
    request = DelegationRequest(prompt="hi")
    with _patch_subprocess("", stderr="auth required", returncode=1), pytest.raises(
        DelegationError
    ):
        await cli.run(request)
