"""Provider protocol: every backend (DeepSeek, Anthropic, mock, …) implements this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from korpha.inference.registry import ProviderAccount
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)


class Provider(ABC):
    """A pluggable LLM backend.

    Subclasses must set `name` and implement `complete`. They MUST NOT mutate
    the account; the router/pool owns account-state transitions.
    """

    name: str

    @abstractmethod
    async def complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> CompletionResponse:
        """Issue a completion using the given account. May raise RateLimitError."""

    async def stream_complete(
        self,
        request: CompletionRequest,
        account: ProviderAccount,
    ) -> AsyncIterator[StreamChunk]:
        """Stream chunks as they arrive.

        Default implementation calls ``complete`` and yields the result as a
        single chunk — providers that genuinely support streaming override
        this with an SSE-aware version.
        """
        response = await self.complete(request, account)
        yield StreamChunk(
            delta_content=response.content,
            delta_reasoning=response.reasoning or "",
            finish_reason=response.finish_reason,
        )


class RateLimitError(Exception):
    """Raised by a Provider when the account hit a rate limit. Router will swap."""

    def __init__(
        self,
        account_id: str,
        retry_after_seconds: float = 60.0,
        *,
        classified: object | None = None,
    ) -> None:
        super().__init__(f"Rate limit on account {account_id}")
        self.account_id = account_id
        self.retry_after_seconds = retry_after_seconds
        self.classified = classified
        """Optional :class:`ClassifiedError` from
        :func:`korpha.inference.errors.classify`. The router can
        read recovery hints (should_compress, should_fallback)
        without re-classifying. ``object`` typing avoids an import
        cycle through this base module."""


class ProviderError(Exception):
    """Generic non-rate-limit provider failure. Router may swap or surface."""

    def __init__(self, *args: object, classified: object | None = None) -> None:
        super().__init__(*args)
        self.classified = classified
        """Optional ClassifiedError; same role as on RateLimitError."""
