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
    """Hit Claude via Anthropic API. Requires ``ANTHROPIC_API_KEY``."""
    import os

    import httpx

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise SkillError(
            "consultant.ask provider=claude requires ANTHROPIC_API_KEY"
        )
    model = os.environ.get(
        "ANTHROPIC_CONSULTANT_MODEL", "claude-sonnet-4-5",
    ).strip()
    prompt = question if not context else (
        f"Context:\n{context}\n\nQuestion:\n{question}"
    )
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": (
            "You are a senior consultant brought in for hard problems "
            "the main team can't fully resolve. Give a concrete answer "
            "with reasoning. Be direct, concise, and committal."
        ),
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
            f"consultant.ask claude transport: {type(exc).__name__}: {exc}"
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
