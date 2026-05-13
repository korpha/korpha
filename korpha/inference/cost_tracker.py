"""CostTracker: thin wrapper over InferencePool that persists Cost rows.

Decouples accounting from the routing core. Pool returns the response;
tracker writes a Cost row in the same transaction the caller supplies a
session for. Lets us unit-test the pool offline and the tracker against
SQLite without coupling either to the other.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sqlmodel import Session

from korpha.audit.model import Cost
from korpha.inference.pool import InferencePool
from korpha.inference.types import CompletionRequest, CompletionResponse, StreamChunk


@dataclass
class CostTracker:
    pool: InferencePool

    async def complete(
        self,
        request: CompletionRequest,
        *,
        session: Session,
        business_id: UUID,
        agent_role_id: UUID | None = None,
        business_unit_id: UUID | None = None,
        task_id: UUID | None = None,
        thread_id: UUID | None = None,
    ) -> CompletionResponse:
        request = self._apply_auxiliary_overrides(request)

        # Hard-stop budget check BEFORE the LLM call. Raises
        # BudgetExceededError if any active policy is over its
        # cap; the call never goes out, no tokens are spent.
        # Failures inside the budget service (DB hiccup, etc.)
        # log + fall through — we never want budget bookkeeping
        # to wedge an active session unrelated to its core
        # purpose.
        try:
            from korpha.budgets import BudgetService
            BudgetService(session).check_before_complete(
                business_id=business_id,
                agent_role_id=agent_role_id,
                business_unit_id=business_unit_id,
                tier=request.tier.value,
            )
        except ImportError:
            pass
        except Exception as exc:
            from korpha.budgets import BudgetExceededError
            if isinstance(exc, BudgetExceededError):
                raise
            import logging
            logging.getLogger(__name__).warning(
                "budget pre-check failed; proceeding without "
                "enforcement: %s", exc,
            )

        response = await self.pool.complete(request)
        cost = Cost(
            business_id=business_id,
            agent_role_id=agent_role_id,
            business_unit_id=business_unit_id,
            task_id=task_id,
            thread_id=thread_id,
            provider=response.provider,
            model=response.model,
            tier=request.tier,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens,
            cost_usd=response.cost_usd,
        )
        session.add(cost)
        session.commit()

        # Post-write check: did this Cost row push us over a cap?
        # If so, pause the policy now so the NEXT call fails
        # cleanly rather than the call after that. Tradeoff: at
        # most one over-cap call per trip vs. checking twice.
        try:
            from korpha.budgets import BudgetService
            BudgetService(session).maybe_pause_after_complete(
                business_id=business_id,
                agent_role_id=agent_role_id,
                business_unit_id=business_unit_id,
                tier=request.tier.value,
            )
        except Exception:  # noqa: BLE001
            pass

        return response

    def _apply_auxiliary_overrides(
        self, request: CompletionRequest,
    ) -> CompletionRequest:
        """Consult ``~/.korpha/auxiliary.yaml`` for per-task tier
        pinning. When a longer prefix matches the request's
        ``session_key``, swap the tier in. Pure passthrough when no
        config exists or no prefix matches — zero behavior change
        for callers without a config file."""
        from dataclasses import replace

        from korpha.inference.auxiliary import load_auxiliary_config

        cfg = load_auxiliary_config()
        if not cfg.tier_overrides:
            return request
        new_tier = cfg.resolve_tier(request.session_key, request.tier)
        if new_tier == request.tier:
            return request
        return replace(request, tier=new_tier)

    async def stream(
        self,
        request: CompletionRequest,
        *,
        session: Session,
        business_id: UUID,
        agent_role_id: UUID | None = None,
        task_id: UUID | None = None,
        thread_id: UUID | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream chunks. Cost is NOT yet persisted on stream paths because
        the OpenAI-compat SSE protocol does not include token usage in the
        delta frames; we'd need to make a separate finalize call to capture
        it. For now the stream path runs uncosted (subscription users on
        Ollama Cloud are unaffected — pricing == 0). Coming back to this
        once we add the optional ``include_usage`` stream extension."""
        request = self._apply_auxiliary_overrides(request)
        async for chunk in self.pool.stream(request):
            yield chunk
