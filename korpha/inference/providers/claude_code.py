"""Claude Code CLI as an Inference Provider.

Mirror of CodexCLIProvider for Anthropic's ``claude`` binary. Lets Mike
use a Claude Pro / Max subscription to power the cofounder loop —
$0 marginal cost, no API key. Auth is whatever ``claude`` keychain /
OAuth state is already on disk (we drop ``--bare`` so the keychain is
read; ``--bare`` would force ANTHROPIC_API_KEY-only).

Output is much cleaner than Codex: ``--print --output-format json``
returns a single JSON object with ``result`` (the response text),
``usage`` (full token breakdown including cache_read / cache_creation),
and ``total_cost_usd`` (zero for subscription accounts, real $ for
API-key-billed accounts).

Trade-offs:
- ✓ Same shape as the Codex provider — auth via the CLI, no PKCE
- ✓ Real cost reporting for both subscription ($0) and API-key billing
- ✓ Cache stats surface so the cost-pill UI can show "saved vs Sonnet"
- ✗ Subprocess overhead per call (~100-200ms vs persistent HTTP)
- ✗ One model per call; can't easily multiplex tiers within a session
"""
from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

from korpha.inference.provider import Provider, ProviderError
from korpha.inference.providers.codex_cli import _flatten_messages
from korpha.inference.registry import ProviderAccount
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)


@dataclass
class ClaudeCodeProvider(Provider):
    """Subprocess-based provider that delegates inference to ``claude``."""

    name: str = "claude-code-cli"
    binary: str = "claude"

    extra_args: tuple[str, ...] = field(default_factory=tuple)
    """Extra flags appended to every claude invocation."""

    async def complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> CompletionResponse:
        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"ClaudeCode account {account.label or account.id} has no "
                f"model mapped for tier {request.tier!s}"
            )
        if shutil.which(self.binary) is None:
            raise ProviderError(
                f"{self.binary!r} not on PATH. Install Claude Code "
                "(curl -fsSL https://claude.ai/install.sh | bash) and "
                "run `claude` once to log in."
            )

        prompt = _flatten_messages(request.messages)
        argv = self._build_argv(model)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=request.timeout_seconds or 120.0,
            )
        except TimeoutError as exc:
            raise ProviderError(
                f"Claude Code timed out after {request.timeout_seconds}s"
            ) from exc

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise ProviderError(
                f"Claude Code exited {proc.returncode}: {err[:500]}"
            )

        body = stdout.decode("utf-8", errors="replace").strip()
        if not body:
            raise ProviderError(
                "Claude Code returned empty stdout. Stderr: "
                + stderr.decode("utf-8", errors="replace")[:300]
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Claude Code returned non-JSON output: {body[:300]}"
            ) from exc

        if payload.get("is_error"):
            # Non-zero exit OR is_error:true — the result field carries
            # the failure message ("Not logged in · Please run /login",
            # "Rate limit exceeded", etc.). Surface it cleanly.
            err_text = str(payload.get("result") or "unknown")
            raise ProviderError(f"Claude Code error: {err_text}")

        text = str(payload.get("result") or "").strip()
        if not text:
            raise ProviderError(
                "Claude Code returned empty 'result' field. "
                f"Body: {body[:300]}"
            )

        usage = payload.get("usage") or {}
        # Anthropic splits inputs across uncached / cache-creation /
        # cache-read. Sum for total input_tokens; surface cache_read as
        # cached_tokens so the cost-pill can show prompt-cache hit rate.
        input_total = (
            int(usage.get("input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
        )
        cached = int(usage.get("cache_read_input_tokens", 0))
        output = int(usage.get("output_tokens", 0))
        cost = Decimal(str(payload.get("total_cost_usd") or 0))

        return CompletionResponse(
            content=text,
            tool_calls=(),
            input_tokens=input_total,
            output_tokens=output,
            cached_tokens=cached,
            cost_usd=cost,
            provider=self.name,
            model=model,
            account_id=str(account.id),
            reasoning=None,
            finish_reason=str(payload.get("stop_reason") or "stop"),
            cache_hit_ratio=cached / max(input_total, 1),
        )

    async def stream_complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> AsyncIterator[StreamChunk]:
        """Stream by parsing ``stream-json`` output line-by-line.

        Claude's stream-json format emits incremental events; we yield
        a chunk whenever a ``content_block_delta`` arrives, then a final
        empty chunk with the finish reason once ``message_stop`` lands.
        """
        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"ClaudeCode account {account.label or account.id} has no "
                f"model mapped for tier {request.tier!s}"
            )
        if shutil.which(self.binary) is None:
            raise ProviderError(
                f"{self.binary!r} not on PATH. Run `claude` once to log in."
            )

        prompt = _flatten_messages(request.messages)
        argv = self._build_argv(model, output_format="stream-json")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdin is not None:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        finish_reason: str | None = None
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev_type = event.get("type")
            if ev_type == "content_block_delta":
                delta = event.get("delta") or {}
                text = str(delta.get("text") or "")
                if text:
                    yield StreamChunk(delta_content=text, raw=event)
            elif ev_type in ("message_stop", "result"):
                finish_reason = "stop"

        await proc.wait()
        yield StreamChunk(finish_reason=finish_reason or "stop")

    def _build_argv(
        self, model: str, *, output_format: str = "json"
    ) -> list[str]:
        argv = [
            self.binary,
            "--print",
            "--output-format",
            output_format,
            "--no-session-persistence",
        ]
        # claude-default sentinel → omit --model, let claude pick based
        # on subscription/account entitlement (mirrors the codex-cli
        # ``codex-default`` pattern).
        if model and model not in ("claude-default", "default", ""):
            argv.extend(["--model", model])
        argv.extend(self.extra_args)
        return argv


__all__ = ["ClaudeCodeProvider"]
