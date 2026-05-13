"""Lightweight observability surface — error reporting + future
metrics hook.

Goal: give production ops a single seam to plug Sentry / Bugsnag /
Honeycomb / OpenTelemetry into without making Korpha depend on
any of them. Default behavior is just ``logging.exception()`` —
exactly what we did before — but the seam exists so plugins can
override.

Usage from anywhere in the codebase::

    from korpha.observability import report_error

    try:
        do_dicey_thing()
    except Exception as exc:
        report_error(exc, context={"skill": "outreach.send"})
        # then handle / re-raise / etc.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

ErrorReporter = Callable[[BaseException, dict[str, Any]], None]
"""Signature for a custom reporter. Plugins can swap in a Sentry
client by calling ``set_error_reporter(...)``."""


def _default_reporter(
    exc: BaseException, context: dict[str, Any],
) -> None:
    """Log to the standard ``logging`` channel with a structured
    extra. Production deploys piping logs to anything (CloudWatch,
    Loki, journald) get the error + context for free."""
    logger.exception(
        "korpha error: %s",
        exc.__class__.__name__,
        extra={"korpha_error_context": context},
    )


_active_reporter: ErrorReporter = _default_reporter


def report_error(
    exc: BaseException,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Hand an unexpected exception to whichever reporter is
    active. Call this on every ``except Exception`` site that
    isn't a normal control-flow path — Sentry will pick it up
    when configured, logs will pick it up otherwise.

    Don't use this for *expected* errors (validation, user
    input). Reserve for "this should never happen" paths."""
    safe_ctx = dict(context or {})
    try:
        _active_reporter(exc, safe_ctx)
    except Exception as _reporter_err:  # noqa: BLE001
        # The reporter itself blew up — fall back to plain
        # logging.exception so the original error doesn't get
        # lost behind a reporter bug.
        logger.exception(
            "error-reporter failed; original error follows: %s",
            exc.__class__.__name__,
        )


def set_error_reporter(reporter: ErrorReporter) -> ErrorReporter:
    """Install a custom reporter (e.g. Sentry's capture_exception
    wrapped to match our signature). Returns the previous reporter
    so callers can restore on shutdown / between tests."""
    global _active_reporter
    previous = _active_reporter
    _active_reporter = reporter
    return previous


def reset_error_reporter() -> None:
    """Tests use this between cases."""
    global _active_reporter
    _active_reporter = _default_reporter


__all__ = [
    "ErrorReporter",
    "report_error",
    "reset_error_reporter",
    "set_error_reporter",
]
