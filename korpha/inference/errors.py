"""API error classification — central taxonomy for inference failures.

Inspired by Hermes' ``agent/error_classifier.py``. Stripped to the
shapes Korpha actually sees today — provider-specific quirks
(Anthropic's thinking-block sigs, llama.cpp grammar errors,
Alibaba's "rate increased too quickly", etc.) get added when they
bite. The kernel handles the common cases:

  - Auth (401 / 403)
  - Billing exhaustion (402 + body patterns)
  - Rate limit (429 + body patterns)
  - Overloaded (503 / 529)
  - Server error (500 / 502 / 504)
  - Timeout (httpx exceptions)
  - Context overflow (400 with size patterns)
  - Format error (400 without size pattern)
  - Model not found (404)
  - Unknown — fallthrough

Each classification carries recovery hints (retryable, should_rotate
credential, should_fallback model, should_compress context) so the
router doesn't re-classify on every retry — read the hints, act.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class FailoverReason(enum.StrEnum):
    """Why an inference call failed → drives recovery strategy."""

    AUTH = "auth"
    """401 / 403 — refresh credentials or rotate to another account."""

    AUTH_PERMANENT = "auth_permanent"
    """Auth still failing after a refresh — abort, surface to user."""

    BILLING = "billing"
    """Account billing exhausted (402, body matches billing patterns).
    Don't retry the same account; rotate immediately."""

    RATE_LIMIT = "rate_limit"
    """429 or quota throttling — backoff then retry / rotate."""

    OVERLOADED = "overloaded"
    """503 / 529 — provider is at capacity; backoff, optionally
    fallback to another provider after a few attempts."""

    SERVER_ERROR = "server_error"
    """500 / 502 / 504 — internal failure; retry with backoff."""

    TIMEOUT = "timeout"
    """Connection or read timeout — rebuild client + retry."""

    CONTEXT_OVERFLOW = "context_overflow"
    """Prompt + context exceeded the model's window — compress
    history, don't retry as-is."""

    MODEL_NOT_FOUND = "model_not_found"
    """404 / invalid model — fallback to a different model in the
    same provider."""

    FORMAT_ERROR = "format_error"
    """400 without a size pattern — typically a malformed request.
    Don't retry; surface the error so the call site can fix it."""

    UNKNOWN = "unknown"
    """Couldn't classify — retry with backoff once or twice, then
    give up."""


@dataclass(frozen=True)
class ClassifiedError:
    """Structured outcome from :func:`classify`. Read the hints —
    don't re-classify."""

    reason: FailoverReason
    status_code: int | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    retryable: bool = True
    should_rotate_credential: bool = False
    """Switch to a different ``ProviderAccount`` for the next attempt."""

    should_fallback: bool = False
    """Try a different model (typically a smaller / faster fallback)."""

    should_compress: bool = False
    """Compress the conversation history before retry. Used for
    context overflow."""

    @property
    def is_auth(self) -> bool:
        return self.reason in (
            FailoverReason.AUTH, FailoverReason.AUTH_PERMANENT,
        )

    @property
    def is_transient(self) -> bool:
        """True for failures that typically resolve on retry without
        any caller intervention. Useful for "should I burn a retry
        on this?" decisions."""
        return self.reason in (
            FailoverReason.RATE_LIMIT,
            FailoverReason.OVERLOADED,
            FailoverReason.SERVER_ERROR,
            FailoverReason.TIMEOUT,
        )


# ----------------------- Body patterns --------------------------------


_BILLING_PATTERNS: tuple[str, ...] = (
    "insufficient credits",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "credits have been exhausted",
    "top up your credits",
    "payment required",
    "billing hard limit",
    "exceeded your current quota",
    "account is deactivated",
    "plan does not include",
)

_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "try again in",
    "please retry after",
    "resource_exhausted",
)

_CONTEXT_OVERFLOW_PATTERNS: tuple[str, ...] = (
    "context_length_exceeded",
    "context length",
    "maximum context",
    "exceeds the maximum",
    "tokens, however the model",
    "prompt is too long",
    "input is too long",
    "context too long",
    "message is too long",
)

_AUTH_PATTERNS: tuple[str, ...] = (
    "invalid api key",
    "incorrect api key",
    "invalid authentication",
    "authentication failed",
    "unauthorized",
    "missing api key",
)

_MODEL_NOT_FOUND_PATTERNS: tuple[str, ...] = (
    "model not found",
    "model_not_found",
    "no such model",
    "unknown model",
    "invalid model",
    "model does not exist",
)


def _matches_any(haystack: str, needles: tuple[str, ...]) -> bool:
    if not haystack:
        return False
    lowered = haystack.lower()
    return any(p in lowered for p in needles)


# ----------------------- Classifier -----------------------------------


def classify(
    *,
    status_code: int | None = None,
    body: str | None = None,
    exc: BaseException | None = None,
) -> ClassifiedError:
    """Classify an inference failure into a recovery strategy.

    Inputs are best-effort:
      - ``status_code`` from the HTTP response
      - ``body`` from the response (str or repr) — pattern-matched
      - ``exc`` from the transport (httpx Timeout, etc.)

    The first signal that matches wins. Order is roughly: auth →
    billing → rate-limit → context overflow → model-not-found →
    overloaded → server error → timeout → format error → unknown.

    Always returns a ClassifiedError; never raises.
    """
    body = body or ""

    # Transport layer first — exceptions trump status codes.
    if exc is not None:
        name = type(exc).__name__.lower()
        if "timeout" in name:
            return ClassifiedError(
                reason=FailoverReason.TIMEOUT,
                status_code=status_code,
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
                should_rotate_credential=False,
            )

    # Status-code-driven branches (with body checks for finer-grained
    # categories).
    sc = status_code
    if sc == 401 or sc == 403:
        return ClassifiedError(
            reason=FailoverReason.AUTH,
            status_code=sc,
            message=body[:300],
            retryable=True,  # Maybe the token expired; rotate
            should_rotate_credential=True,
        )

    if sc == 402 or _matches_any(body, _BILLING_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.BILLING,
            status_code=sc,
            message=body[:300],
            retryable=True,
            should_rotate_credential=True,
        )

    if sc == 429 or _matches_any(body, _RATE_LIMIT_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.RATE_LIMIT,
            status_code=sc,
            message=body[:300],
            retryable=True,
            should_rotate_credential=True,
        )

    if sc == 400 and _matches_any(body, _CONTEXT_OVERFLOW_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.CONTEXT_OVERFLOW,
            status_code=sc,
            message=body[:300],
            retryable=True,
            should_compress=True,
        )

    if sc == 404 or _matches_any(body, _MODEL_NOT_FOUND_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.MODEL_NOT_FOUND,
            status_code=sc,
            message=body[:300],
            retryable=True,
            should_fallback=True,
        )

    if sc in (503, 529):
        return ClassifiedError(
            reason=FailoverReason.OVERLOADED,
            status_code=sc,
            message=body[:300],
            retryable=True,
        )

    if sc in (500, 502, 504):
        return ClassifiedError(
            reason=FailoverReason.SERVER_ERROR,
            status_code=sc,
            message=body[:300],
            retryable=True,
        )

    if sc == 400:
        return ClassifiedError(
            reason=FailoverReason.FORMAT_ERROR,
            status_code=sc,
            message=body[:300],
            retryable=False,
        )

    # Body-pattern fallbacks for providers that return non-standard
    # status codes (looking at you, llama.cpp).
    if _matches_any(body, _AUTH_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.AUTH,
            status_code=sc,
            message=body[:300],
            retryable=True,
            should_rotate_credential=True,
        )

    return ClassifiedError(
        reason=FailoverReason.UNKNOWN,
        status_code=sc,
        message=body[:300],
        retryable=True,
    )


__all__ = [
    "ClassifiedError",
    "FailoverReason",
    "classify",
]
