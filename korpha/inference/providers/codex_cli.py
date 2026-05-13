"""Codex CLI as an Inference Provider.

Lets Mike use his ChatGPT Plus / Pro subscription to power the cofounder
loop with $0 marginal cost — no API key. We subprocess ``codex exec --json``
and parse the NDJSON event stream into our standard CompletionResponse /
StreamChunk shapes.

Why not OAuth-tokens-via-HTTP (the OpenClaw pattern)? Codex CLI's OAuth
token may not be valid for arbitrary OpenAI Chat Completions outside its
own API context, and the token-management surface (PKCE + refresh +
storage) is non-trivial. Shelling out to the binary that already handles
all of that is dramatically simpler — auth = "did Mike run codex login?".

Trade-offs:
- ✓ No token storage, no refresh handling, no PKCE
- ✓ Same pattern works for Claude Code (subscription auth via `claude` CLI)
- ✗ Subprocess overhead per call (~100-200ms vs persistent HTTP)
- ✗ No prompt-cache affinity across calls (each subprocess starts fresh)
- ✗ Codex sandbox/git-repo guard rails apply even for pure inference

For Mike's first month the latency is fine. Power users graduate to
direct API keys via the standard OpenAI preset.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from korpha.inference.provider import Provider, ProviderError
from korpha.inference.registry import ProviderAccount
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    Role,
    StreamChunk,
)


@dataclass
class CodexCLIProvider(Provider):
    """Subprocess-based provider that delegates inference to ``codex exec``."""

    name: str = "codex-cli"
    binary: str = "codex"
    """Override for testing — point at a script that emits canned NDJSON."""

    sandbox_mode: str = "read-only"
    """Codex sandbox policy. ``read-only`` is safe for pure inference —
    the model can think but can't write files or run commands."""

    extra_args: tuple[str, ...] = field(default_factory=tuple)
    """Extra flags appended to every codex invocation. Useful for tests
    or for forcing config overrides via ``-c key=value``."""

    async def complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> CompletionResponse:
        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"CodexCLI account {account.label or account.id} has no "
                f"model mapped for tier {request.tier!s}"
            )
        if shutil.which(self.binary) is None:
            raise ProviderError(
                f"{self.binary!r} not on PATH. Install Codex CLI "
                "(npm install -g @openai/codex) and run `codex login`."
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
                f"Codex CLI timed out after {request.timeout_seconds}s"
            ) from exc

        # Codex sometimes exits non-zero AFTER emitting a complete
        # response, e.g. with a cosmetic "failed to record rollout
        # items" error post-completion. Parse stdout first and only
        # treat a non-zero exit as fatal when we got nothing useful —
        # otherwise the response we paid tokens for would be discarded.
        text, usage = _parse_ndjson(stdout.decode("utf-8", errors="replace"))
        if proc.returncode != 0 and not text:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise ProviderError(
                f"Codex CLI exited {proc.returncode}: {err[:500]}"
            )
        if not text:
            raise ProviderError(
                "Codex CLI returned no agent_message events. Stderr: "
                + stderr.decode("utf-8", errors="replace")[:300]
            )

        return CompletionResponse(
            content=text,
            tool_calls=(),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cached_input_tokens", 0),
            # Subscription-paid — no per-call dollar cost. The tracker
            # may still display token counts.
            cost_usd=Decimal("0"),
            provider=self.name,
            model=model,
            account_id=str(account.id),
            reasoning=None,
            finish_reason="stop",
            cache_hit_ratio=(
                usage.get("cached_input_tokens", 0)
                / max(usage.get("input_tokens", 1), 1)
            ),
        )

    async def stream_complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> AsyncIterator[StreamChunk]:
        """Stream by parsing NDJSON line-by-line as Codex emits it."""
        model = account.tier_models.get(request.tier)
        if model is None:
            raise ProviderError(
                f"CodexCLI account {account.label or account.id} has no "
                f"model mapped for tier {request.tier!s}"
            )
        if shutil.which(self.binary) is None:
            raise ProviderError(
                f"{self.binary!r} not on PATH. Run `codex login` first."
            )

        prompt = _flatten_messages(request.messages)
        argv = self._build_argv(model)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Push the prompt and close stdin so codex starts processing.
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
            if ev_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text = str(item.get("text") or "")
                    if text:
                        yield StreamChunk(delta_content=text, raw=event)
            elif ev_type == "turn.completed":
                finish_reason = "stop"

        await proc.wait()
        if finish_reason is None and proc.returncode != 0:
            finish_reason = "error"
        # Final empty chunk to carry the finish reason — matches the
        # OpenAI-compat provider's last-frame convention.
        yield StreamChunk(finish_reason=finish_reason or "stop")

    def _build_argv(self, model: str) -> list[str]:
        argv = [
            self.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-s",
            self.sandbox_mode,
        ]
        # Codex with a ChatGPT-account login only accepts certain model
        # names (e.g. gpt-5.5-codex) and rejects e.g. plain "gpt-5". The
        # subscription routes inside Codex pick a default automatically;
        # to let it do that, treat ``codex-default`` (or empty) as
        # "don't pin a model — let Codex choose".
        if model and model not in ("codex-default", "default", ""):
            argv.extend(["-m", model])
        argv.extend(self.extra_args)
        argv.append("-")  # read prompt from stdin
        return argv


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _flatten_messages(messages: list[Message]) -> str:
    """Codex takes a single prompt, not a chat-completion message array.

    We render messages as plain-text turns. System content sits at the
    top as instructions; user/assistant turns alternate beneath. Tool
    messages are rendered as `[tool: <content>]` since Codex doesn't
    have a first-class tool-message concept.
    """
    blocks: list[str] = []
    system_chunks: list[str] = []
    for m in messages:
        if m.role == Role.SYSTEM:
            system_chunks.append(m.content)
    if system_chunks:
        blocks.append("<system>\n" + "\n\n".join(system_chunks) + "\n</system>")
    for m in messages:
        if m.role == Role.SYSTEM:
            continue
        if m.role == Role.USER:
            blocks.append(f"User: {m.content}")
        elif m.role == Role.ASSISTANT:
            blocks.append(f"Assistant: {m.content}")
        elif m.role == Role.TOOL:
            blocks.append(f"[tool result: {m.content}]")
    return "\n\n".join(blocks)


def _parse_ndjson(body: str) -> tuple[str, dict[str, Any]]:
    """Concatenate every ``agent_message.text`` and pull the final
    ``turn.completed.usage`` block. Returns (text, usage_dict)."""
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text_parts.append(str(item.get("text") or ""))
        elif event.get("type") == "turn.completed":
            u = event.get("usage")
            if isinstance(u, dict):
                usage = u
    return "\n".join(text_parts).strip(), usage


__all__ = ["CodexCLIProvider"]
