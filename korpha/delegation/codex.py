"""Codex CLI wrapper.

Shells out to ``codex exec "prompt"`` non-interactively. The Codex CLI uses
the user's ChatGPT subscription locally — no per-token billing concern when
running OSS on the user's machine.

Codex's non-interactive output is currently text-mode by default; we capture
stdout and surface it as the response content. Token / cost accounting is
not exposed by the CLI so we leave those fields zero (subscription-paid).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass

from korpha.delegation.types import (
    DelegationError,
    DelegationRequest,
    DelegationResponse,
    DelegationTimeout,
)


@dataclass
class CodexCLI:
    binary: str = "codex"
    model: str | None = None
    """Optional --model override. None = use Codex default."""

    sandbox_mode: str | None = "read-only"
    """Codex sandbox policy. read-only is the safe default; bump to write
    only when actually delegating code edits."""

    extra_default_args: tuple[str, ...] = ()

    async def run(self, request: DelegationRequest) -> DelegationResponse:
        args = [self.binary, "exec"]
        if self.sandbox_mode is not None:
            args.extend(["--sandbox", self.sandbox_mode])
        if self.model is not None:
            args.extend(["--model", self.model])
        args.extend(self.extra_default_args)
        args.extend(request.extra_args)
        args.append(request.prompt)

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
                f"Codex CLI timed out after {request.timeout_seconds}s"
            ) from exc

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise DelegationError(
                f"Codex exited with {proc.returncode}: {stderr[:500] or stdout[:500]}"
            )

        return DelegationResponse(
            content=stdout.strip(),
            raw_output=stdout,
            is_error=False,
        )
