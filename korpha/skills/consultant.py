"""``consultant.ask`` — explicit cross-model consultant call.

When a Director (running on DeepSeek as the main runtime) hits a hard
problem and wants a second opinion from a smarter model, they call
this skill. The call goes straight to the configured consultant
provider (default: Codex / gpt-5.4 via the user's ChatGPT
subscription) — bypassing the main inference cascade so it doesn't
silently shift the whole team onto the consultant's quota.

Design intent (per memory rule):

  - **Main inference** stays open-weights (DeepSeek), cheap + fast.
  - **Consultant** is a discrete call-out — explicit skill invocation,
    not a default routing change. Hits the founder's ChatGPT
    subscription only when the team explicitly asks for it.
  - **Image gen + web search** are also subscription-paid via the
    same OAuth — these already auto-route through Codex by default
    because they're tool calls, not chat turns.

Surfaces both via the CEO router (chain step) and as a direct skill
the team can compose into other skills (e.g. a CTO ``review_plan``
skill could chain ``consultant.ask`` for an architecture review).

Tracks usage in :class:`CostLog` with ``actor_type=CONSULTANT`` so
the founder can see exactly how much consultant time is being spent
on /app/insights, separate from main inference spend.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

from korpha.audit.model import InferenceTier
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)

logger = logging.getLogger(__name__)


# Default model when the caller doesn't pin one. ``gpt-5.4`` is the
# chat host for the Codex Responses surface — same model the codex
# CLI itself uses.
_DEFAULT_CODEX_MODEL = "gpt-5.4"


class ConsultantAskSkill(Skill):
    """One-shot ask to the consultant model. Returns the answer text."""

    spec = SkillSpec(
        name="consultant.ask",
        description=(
            "Ask the consultant model (gpt-5.4 via your ChatGPT "
            "subscription) for a one-shot answer. Use this for hard "
            "reasoning, architecture review, second-opinion on a "
            "tradeoff, or any question where the main DeepSeek brain "
            "feels under-resourced. Bypasses the inference cascade — "
            "this is an explicit call-out, not a routing change. "
            "Cost: ~$0 marginal (subscription-paid) but consumes "
            "Plus quota — agents should only invoke when the main "
            "model genuinely can't get there alone."
        ),
        parameters={
            "question": (
                "The question to ask the consultant. Be specific. "
                "Include relevant context inline (card body, recent "
                "decisions, options being weighed)."
            ),
            "context": (
                "Optional. Additional context that helps the "
                "consultant answer — recent thread, file contents, "
                "tradeoff frame. Prepended to the question with a "
                "'Context:' header. Keep under ~8k chars."
            ),
            "provider": (
                "Optional. 'codex' (default, ChatGPT subscription) "
                "or 'claude' (Claude Pro subscription, requires "
                "ANTHROPIC_API_KEY). Future: 'gemini', 'kimi', etc."
            ),
        },
        default_tier=InferenceTier.CONSULTANT,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        question = str(args.get("question") or "").strip()
        if not question:
            raise SkillError("consultant.ask: question is required")
        context_text = str(args.get("context") or "").strip()
        provider_name = str(args.get("provider") or "codex").strip().lower()

        if provider_name not in {"codex", "claude"}:
            raise SkillError(
                f"consultant.ask: provider {provider_name!r} not "
                "supported yet (use 'codex' or 'claude')"
            )

        if provider_name == "codex":
            answer, model_used, in_tok, out_tok = await _ask_codex(
                question=question, context=context_text,
            )
        else:
            answer, model_used, in_tok, out_tok = await _ask_claude(
                question=question, context=context_text,
            )

        # Audit log so /app/insights can show consultant usage
        # separately from main inference. Cost is $0 marginal for
        # both subscription providers; the tracker still records
        # token counts under tier=CONSULTANT for spend visibility.
        try:
            from korpha.audit.model import Cost
            cost_row = Cost(
                business_id=ctx.business.id,
                agent_role_id=ctx.invoking_agent_role_id,
                provider=provider_name,
                model=model_used,
                tier=InferenceTier.CONSULTANT,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=Decimal("0"),
            )
            ctx.session.add(cost_row)
            ctx.session.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "consultant.ask: cost logging failed", exc_info=True,
            )

        return SkillResult(
            skill_name="consultant.ask",
            summary=(
                f"consultant.ask via {provider_name}: {len(answer)} char answer"
            ),
            payload={
                "question": question,
                "answer": answer,
                "provider": provider_name,
                "model": model_used,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
        )


async def _ask_codex(
    *, question: str, context: str,
) -> tuple[str, str, int, int]:
    """Hit the Codex Responses surface directly."""
    from korpha.audit.model import InferenceTier as _Tier
    from korpha.inference.providers.codex_responses import (
        CodexResponsesProvider,
    )
    from korpha.inference.registry import AuthType, ProviderAccount
    from korpha.inference.types import CompletionRequest, Message, Role

    provider = CodexResponsesProvider()
    account = ProviderAccount(
        id=uuid4(),
        provider_name="codex-cli",
        label="consultant-codex",
        auth_type=AuthType.SUBSCRIPTION_CLI,
        api_key="subscription",
        tier_models={_Tier.CONSULTANT: _DEFAULT_CODEX_MODEL},
        priority=0,
    )
    prompt = question if not context else (
        f"Context:\n{context}\n\nQuestion:\n{question}"
    )
    request = CompletionRequest(
        messages=[
            Message(
                role=Role.SYSTEM,
                content=(
                    "You are a senior consultant brought in for hard "
                    "problems the main team can't fully resolve. Give "
                    "a concrete answer with reasoning. Be direct, "
                    "concise, and committal — don't hedge with 'it "
                    "depends' unless the question genuinely is "
                    "either/or with a hard tradeoff."
                ),
            ),
            Message(role=Role.USER, content=prompt),
        ],
        tier=_Tier.CONSULTANT,
        session_key=f"consultant-{uuid4().hex[:8]}",
    )
    response = await provider.complete(request, account)
    return (
        response.content,
        response.model,
        response.input_tokens,
        response.output_tokens,
    )


async def _ask_claude(
    *, question: str, context: str,
) -> tuple[str, str, int, int]:
    """Hit Claude via subscription (Claude Code CLI) or API key.

    **Subscription path (preferred when ``claude`` CLI is logged in)** —
    subprocess ``claude --print --output-format=json`` reads the OAuth
    state Claude Code already manages. $0 marginal for Pro/Max users.

    **API path (fallback when no CLI but ``ANTHROPIC_API_KEY`` set)** —
    direct POST to Anthropic Messages API. For users without a Pro
    subscription. Pay-per-token billing.

    The Agent SDK (``pip install claude-agent-sdk``) is NOT used —
    Anthropic blocks subscription auth on the SDK; only API keys work.
    The CLI subprocess path is the only viable subscription route.
    """
    import os
    import shutil

    cli_path = shutil.which("claude")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    prompt = question if not context else (
        f"Context:\n{context}\n\nQuestion:\n{question}"
    )
    system_prompt = (
        "You are a senior consultant brought in for hard problems "
        "the main team can't fully resolve. Give a concrete answer "
        "with reasoning. Be direct, concise, and committal."
    )

    # Prefer subscription (CLI) when available — $0 marginal for the
    # founder. The API path is the fallback for users without a Pro
    # plan but with API access.
    if cli_path:
        return await _ask_claude_cli(prompt, system_prompt)
    if api_key:
        return await _ask_claude_api(prompt, system_prompt, api_key)
    raise SkillError(
        "consultant.ask provider=claude needs either the `claude` CLI "
        "logged in (run `claude` once) or ANTHROPIC_API_KEY set"
    )


async def _ask_claude_cli(
    prompt: str, system: str,
) -> tuple[str, str, int, int]:
    """Subprocess the Claude Code CLI. Uses the OAuth `claude login`
    set up, so the founder's Pro / Max subscription pays."""
    import asyncio
    import json
    import os

    model = os.environ.get(
        "ANTHROPIC_CONSULTANT_MODEL", "sonnet",
    ).strip()
    argv = [
        "claude",
        "--print",
        "--output-format=json",
        "--model", model,
        "--append-system-prompt", system,
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")),
            timeout=120.0,
        )
    except TimeoutError as exc:
        proc.kill()
        raise SkillError("claude CLI timed out after 120s") from exc

    if proc.returncode != 0:
        raise SkillError(
            f"claude CLI exited {proc.returncode}: "
            + stderr.decode("utf-8", errors="replace")[:300]
        )
    body = stdout.decode("utf-8", errors="replace").strip()
    if not body:
        raise SkillError(
            "claude CLI returned empty stdout: "
            + stderr.decode("utf-8", errors="replace")[:300]
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SkillError(
            f"claude CLI returned non-JSON: {body[:300]}"
        ) from exc
    if payload.get("is_error"):
        raise SkillError(
            f"claude error: {payload.get('result') or 'unknown'}"
        )
    text = str(payload.get("result") or "").strip()
    if not text:
        raise SkillError("claude CLI returned empty result field")
    usage = payload.get("usage") or {}
    in_tok = (
        int(usage.get("input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0))
        + int(usage.get("cache_read_input_tokens", 0))
    )
    out_tok = int(usage.get("output_tokens") or 0)
    return text, f"claude-{model}", in_tok, out_tok


async def _ask_claude_api(
    prompt: str, system: str, key: str,
) -> tuple[str, str, int, int]:
    """Direct Anthropic Messages API — only for users without a CLI
    install but with a paid API key."""
    import os

    import httpx

    model = os.environ.get(
        "ANTHROPIC_CONSULTANT_MODEL", "claude-sonnet-4-5",
    ).strip()
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise SkillError(
            f"claude API transport: {type(exc).__name__}: {exc}"
        ) from exc
    answer = "".join(
        b.get("text", "") for b in (data.get("content") or [])
        if b.get("type") == "text"
    )
    usage = data.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    return answer.strip(), model, in_tok, out_tok


def register_skills() -> None:
    register(ConsultantAskSkill())


__all__ = ["ConsultantAskSkill", "register_skills"]
