"""Claude Code CLI wrapper.

Shells out to ``claude -p "prompt" --output-format json`` and parses the
single-result JSON shape. Used for Consultant tier escalation and for
delegated coding work.

Auth is whatever the local ``claude`` binary already has — Claude Pro
login via ``claude login``, or an API key in the binary's environment.
We don't manage credentials ourselves; we just shell out.

Caller MUST pass ``max_budget_usd`` to bound spend per invocation.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from korpha.delegation.types import (
    DelegationBudgetExceeded,
    DelegationError,
    DelegationRequest,
    DelegationResponse,
    DelegationTimeout,
)


@dataclass
class ClaudeCodeCLI:
    binary: str = "claude"
    skip_permissions: bool = True
    """Pass --allow-dangerously-skip-permissions. Default on for self-hosted
    use so the cofounder can act without prompting at every tool call.
    Disable for untrusted contexts."""

    extra_default_args: tuple[str, ...] = ()

    async def run(self, request: DelegationRequest) -> DelegationResponse:
        if request.max_budget_usd is None:
            raise DelegationError(
                "ClaudeCodeCLI requires DelegationRequest.max_budget_usd "
                "(it controls cost — never call without a cap)."
            )

        args = [
            self.binary,
            "-p",
            request.prompt,
            "--output-format",
            "json",
            "--max-budget-usd",
            str(request.max_budget_usd),
        ]
        if self.skip_permissions:
            args.append("--allow-dangerously-skip-permissions")
        args.extend(self.extra_default_args)
        args.extend(request.extra_args)

        env = os.environ.copy()
        env.update(request.env_overrides)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=request.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout_seconds
            )
        except TimeoutError as exc:
            with contextlib.suppress(Exception):
                proc.kill()
            raise DelegationTimeout(
                f"Claude Code timed out after {request.timeout_seconds}s"
            ) from exc

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0 and not stdout.strip():
            raise DelegationError(
                f"Claude Code exited with {proc.returncode}: {stderr[:500]}"
            )

        return _parse_claude_json(stdout)


def _parse_claude_json(stdout: str) -> DelegationResponse:
    """Parse ``--output-format json`` single-result shape:

    Success:
        {"type":"result","subtype":"success","is_error":false,"result":"...",
         "duration_ms":...,"session_id":"...","total_cost_usd":...,
         "usage":{"input_tokens":...,"output_tokens":...,
                  "cache_read_input_tokens":...,"cache_creation_input_tokens":...}}

    Budget hit:
        {"type":"result","subtype":"error_max_budget_usd","is_error":true,
         "errors":["Reached maximum budget ..."], ...}
    """
    stdout = stdout.strip()
    if not stdout:
        raise DelegationError("Claude Code returned empty stdout")

    # Some claude versions emit prelude lines; --output-format json emits a
    # single-line JSON object. Find the last line that starts with `{` and
    # parses cleanly.
    data: dict[str, Any] | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if data is None:
        # Fall back: try the whole stdout in one go (covers the no-prelude case).
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DelegationError(f"Claude Code output not JSON: {stdout[:200]}") from exc

    is_error = bool(data.get("is_error", False))
    subtype = data.get("subtype", "")
    cost = Decimal(str(data.get("total_cost_usd", 0) or 0))
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cached_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)

    if is_error and subtype == "error_max_budget_usd":
        raise DelegationBudgetExceeded(
            message=str(data.get("errors", ["budget exceeded"])[0]),
            cost_usd=cost,
        )

    content = str(data.get("result", "") or "")
    error_msg = None
    if is_error:
        errors = data.get("errors") or []
        error_msg = "; ".join(str(e) for e in errors) if errors else subtype or "unknown error"

    return DelegationResponse(
        content=content,
        raw_output=stdout,
        is_error=is_error,
        error_message=error_msg,
        duration_ms=int(data.get("duration_ms", 0) or 0),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cost_usd=cost,
        session_id=data.get("session_id"),
    )
