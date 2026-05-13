"""OAuth CLI subprocess invocation.

PR-INT-5 — actually runs ``claude-code`` / ``codex-cli`` / ``opencode``
binaries via subprocess for Pro-tier LLM calls in local mode. The CLI
binary handles its own OAuth dance (token stored under
~/.config/<cli>/auth.json or similar); this wrapper just calls the
binary with a prompt and captures stdout.

Subprocess invocation pattern:

    invoke_oauth_cli(
        resource=cli_resource,
        prompt="Draft three subject lines for an Korpha launch",
        unit_id=ctx.business_unit_id,
        session=ctx.session,
    )

Tracks quota usage automatically via ``record_oauth_call``. Caller
checks ``OAuthCliQuotaExhausted`` to fall back to API path.

Tests use ``CLI_INVOKER_OVERRIDE`` to inject a mocked subprocess
runner so they don't depend on the binary being installed.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from sqlmodel import Session

from korpha.shared_resources.model import SharedResource
from korpha.shared_resources.oauth_cli import (
    OAuthCliQuotaExhausted, record_oauth_call,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OAuthCliResult:
    """Output from an OAuth CLI subprocess invocation."""

    stdout: str
    stderr: str
    exit_code: int
    cli_name: str


# Test injection hook — when set, ``invoke_oauth_cli`` calls this
# instead of running an actual subprocess. Tests set + clear it via
# fixture; production never touches it.
CLI_INVOKER_OVERRIDE: Callable[..., OAuthCliResult] | None = None


async def invoke_oauth_cli(
    *,
    resource: SharedResource,
    prompt: str,
    session: Session,
    unit_id: UUID,
    skill_name: str | None = None,
    timeout_seconds: int = 120,
) -> OAuthCliResult:
    """Run the OAuth CLI binary with the prompt; capture stdout.

    Records the call against the resource's quota window before
    returning. If the quota is already exhausted (caller didn't check
    first), raises ``OAuthCliQuotaExhausted`` instead of running.

    The actual binary invocation pattern varies per CLI; we treat
    them uniformly as "echo prompt to stdin, read stdout". The
    canonical CLIs (claude-code, codex-cli, opencode) all accept
    a ``-p <prompt>`` flag for prompt-only mode + print response;
    we use that.
    """
    if resource.quota_calls_in_window >= (
        resource.quota_limit_in_window or float("inf")
    ):
        raise OAuthCliQuotaExhausted(
            f"OAuth CLI {resource.name} quota exhausted "
            f"({resource.quota_calls_in_window} calls in window)"
        )

    if CLI_INVOKER_OVERRIDE is not None:
        result = CLI_INVOKER_OVERRIDE(
            resource=resource, prompt=prompt,
            unit_id=unit_id, session=session,
        )
        record_oauth_call(
            session, resource=resource,
            consumer_unit_id=unit_id, skill_name=skill_name,
        )
        return result

    # Resource.name is the registered label (e.g. 'claude-code'); the
    # actual shell binary may differ (e.g. 'claude'). Use the canonical
    # mapping from oauth_cli.py.
    from korpha.shared_resources.oauth_cli import resolve_oauth_cli_binary
    binary_name = resolve_oauth_cli_binary(resource.name)
    binary = shutil.which(binary_name)
    if binary is None:
        raise FileNotFoundError(
            f"OAuth CLI binary {binary_name!r} (for resource "
            f"{resource.name!r}) not on $PATH"
        )

    # All canonical CLIs support -p <prompt> + print + exit.
    proc = await asyncio.create_subprocess_exec(
        binary, "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    result = OAuthCliResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        exit_code=proc.returncode or 0,
        cli_name=resource.name,
    )

    record_oauth_call(
        session, resource=resource,
        consumer_unit_id=unit_id, skill_name=skill_name,
    )
    return result


__all__ = [
    "CLI_INVOKER_OVERRIDE",
    "OAuthCliResult",
    "invoke_oauth_cli",
]
