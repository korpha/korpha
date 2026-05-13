"""Tests for ``korpha.inference.errors`` (classifier) +
``korpha.inference.retry`` (jittered backoff).

Each provider failure mode gets a classification test so we know
the dispatch logic in openai_compat.py routes the right exception
type. The integration test confirms ProviderError / RateLimitError
now carry the structured classification on the ``classified``
attribute.
"""
from __future__ import annotations

import time

import httpx
import pytest

from korpha.inference.errors import (
    ClassifiedError,
    FailoverReason,
    classify,
)
from korpha.inference.retry import jittered_backoff


# ---- classify: status-code branches ----


def test_classify_401_is_auth() -> None:
    r = classify(status_code=401, body="invalid api key")
    assert r.reason == FailoverReason.AUTH
    assert r.is_auth is True
    assert r.should_rotate_credential is True


def test_classify_403_is_auth() -> None:
    r = classify(status_code=403)
    assert r.reason == FailoverReason.AUTH
    assert r.should_rotate_credential is True


def test_classify_402_is_billing() -> None:
    r = classify(status_code=402, body="payment required")
    assert r.reason == FailoverReason.BILLING
    assert r.should_rotate_credential is True


def test_classify_billing_pattern_in_body_without_402() -> None:
    """Some providers return 200/400 with a billing-exhausted body."""
    r = classify(status_code=400, body="Insufficient credits on this account")
    assert r.reason == FailoverReason.BILLING


def test_classify_429_is_rate_limit() -> None:
    r = classify(status_code=429)
    assert r.reason == FailoverReason.RATE_LIMIT
    assert r.is_transient is True
    assert r.should_rotate_credential is True


def test_classify_rate_limit_pattern_in_body() -> None:
    r = classify(status_code=200, body="error: too many requests, please retry")
    assert r.reason == FailoverReason.RATE_LIMIT


def test_classify_503_is_overloaded() -> None:
    r = classify(status_code=503)
    assert r.reason == FailoverReason.OVERLOADED
    assert r.is_transient is True


def test_classify_529_is_overloaded() -> None:
    r = classify(status_code=529)
    assert r.reason == FailoverReason.OVERLOADED


def test_classify_500_is_server_error() -> None:
    r = classify(status_code=500)
    assert r.reason == FailoverReason.SERVER_ERROR
    assert r.is_transient is True


def test_classify_502_is_server_error() -> None:
    r = classify(status_code=502)
    assert r.reason == FailoverReason.SERVER_ERROR


def test_classify_504_is_server_error() -> None:
    r = classify(status_code=504)
    assert r.reason == FailoverReason.SERVER_ERROR


def test_classify_400_with_context_overflow_body() -> None:
    r = classify(
        status_code=400,
        body="This model's maximum context length is 32768 tokens, however the model received...",
    )
    assert r.reason == FailoverReason.CONTEXT_OVERFLOW
    assert r.should_compress is True
    assert r.should_rotate_credential is False


def test_classify_400_without_size_pattern_is_format_error() -> None:
    """A 400 that doesn't match context-overflow / billing / etc.
    is a real format problem — don't retry."""
    r = classify(status_code=400, body="Field 'temperature' must be a number")
    assert r.reason == FailoverReason.FORMAT_ERROR
    assert r.retryable is False


def test_classify_404_is_model_not_found() -> None:
    r = classify(status_code=404, body="The model 'gpt-99' does not exist")
    assert r.reason == FailoverReason.MODEL_NOT_FOUND
    assert r.should_fallback is True


def test_classify_unknown_status_falls_through_to_unknown() -> None:
    r = classify(status_code=418)  # i'm a teapot
    assert r.reason == FailoverReason.UNKNOWN
    assert r.retryable is True


# ---- classify: transport exceptions ----


def test_classify_timeout_exception() -> None:
    exc = httpx.ReadTimeout("read timed out")
    r = classify(exc=exc)
    assert r.reason == FailoverReason.TIMEOUT
    assert r.retryable is True
    assert r.should_rotate_credential is False


# ---- classify: body-only patterns when status code missing ----


def test_classify_auth_pattern_body_only() -> None:
    """A non-standard status code (200) with auth-failure body —
    fallback pattern matching catches it."""
    r = classify(status_code=200, body="Invalid API key provided")
    assert r.reason == FailoverReason.AUTH


def test_classify_no_signal_returns_unknown() -> None:
    r = classify()
    assert r.reason == FailoverReason.UNKNOWN


# ---- classify: priority ordering ----


def test_classify_auth_beats_billing_when_status_is_401() -> None:
    """A 401 with body 'insufficient credits' should still be AUTH —
    the status code wins over the body for auth/billing ambiguity."""
    r = classify(status_code=401, body="insufficient credits")
    assert r.reason == FailoverReason.AUTH


def test_classify_billing_pattern_beats_overloaded_status() -> None:
    """If the body says 'billing exhausted' but the status is 503,
    we want BILLING (rotate immediately, no retry on this account)
    not OVERLOADED (just backoff). Our current ordering says
    BILLING is checked before OVERLOADED — verify that holds."""
    r = classify(
        status_code=503, body="account is deactivated",
    )
    # NOTE: in our current ordering, status-code 503 won't match
    # billing-status branch, but the body matches _BILLING_PATTERNS
    # which IS checked before status==503. So this should be BILLING.
    assert r.reason == FailoverReason.BILLING


# ---- ClassifiedError convenience props ----


def test_classified_error_is_transient_set() -> None:
    rate = classify(status_code=429)
    assert rate.is_transient is True
    auth = classify(status_code=401)
    assert auth.is_transient is False  # not transient — needs intervention


def test_classified_error_extra_dict_field_default() -> None:
    """ClassifiedError.extra defaults to an empty dict (not None)."""
    r = classify(status_code=500)
    assert r.extra == {}


# ---- retry: jittered_backoff ----


def test_jittered_backoff_increases_with_attempt() -> None:
    """Attempt N's *minimum* delay is at least the previous attempt's
    minimum — exponential growth dominates the jitter."""
    d1 = jittered_backoff(1, base_delay=1.0, max_delay=1000.0, jitter_ratio=0)
    d2 = jittered_backoff(2, base_delay=1.0, max_delay=1000.0, jitter_ratio=0)
    d3 = jittered_backoff(3, base_delay=1.0, max_delay=1000.0, jitter_ratio=0)
    assert d1 == 1.0
    assert d2 == 2.0
    assert d3 == 4.0


def test_jittered_backoff_caps_at_max_delay() -> None:
    """Even with a huge attempt number, never exceed max_delay (plus
    a jitter slop)."""
    d = jittered_backoff(20, base_delay=1.0, max_delay=10.0, jitter_ratio=0.5)
    # delay <= max_delay + 0.5*max_delay
    assert d <= 15.1


def test_jittered_backoff_zero_attempt_returns_base() -> None:
    """attempt<=0 collapses to no exponent; exponent = max(0, attempt-1)."""
    d = jittered_backoff(0, base_delay=2.0, max_delay=100.0, jitter_ratio=0)
    assert d == 2.0


def test_jittered_backoff_decorrelates_concurrent_calls() -> None:
    """Two calls in rapid succession with the same parameters should
    return DIFFERENT values (jitter is actually random per call)."""
    samples = [
        jittered_backoff(3, base_delay=1.0, max_delay=100.0, jitter_ratio=0.5)
        for _ in range(10)
    ]
    # Not all identical
    assert len(set(samples)) > 1


def test_jittered_backoff_extreme_attempt_does_not_overflow() -> None:
    """attempt=99 means 2**98 — must not overflow into a wonky
    delay. The function caps at max_delay before the multiply."""
    d = jittered_backoff(99, base_delay=1.0, max_delay=120.0, jitter_ratio=0.5)
    assert d <= 180.0


# ---- integration: ProviderError carries ClassifiedError ----


def test_provider_error_carries_classified_attr() -> None:
    """Smoke that the new classified= kwarg flows through ProviderError."""
    from korpha.inference.provider import ProviderError

    classified = classify(status_code=500)
    err = ProviderError("boom", classified=classified)
    assert err.classified is classified
    assert err.classified.reason == FailoverReason.SERVER_ERROR


def test_rate_limit_error_carries_classified_attr() -> None:
    from korpha.inference.provider import RateLimitError

    classified = classify(status_code=429)
    err = RateLimitError(account_id="x", classified=classified)
    assert err.classified is classified
    assert err.classified.reason == FailoverReason.RATE_LIMIT


def test_provider_error_classified_optional() -> None:
    """Existing call sites that don't pass classified still work."""
    from korpha.inference.provider import ProviderError

    err = ProviderError("legacy")
    assert err.classified is None


def test_rate_limit_error_classified_optional() -> None:
    from korpha.inference.provider import RateLimitError

    err = RateLimitError(account_id="x")
    assert err.classified is None
