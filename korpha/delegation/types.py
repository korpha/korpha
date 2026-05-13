"""Shared types for delegation CLI wrappers."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class DelegationRequest:
    prompt: str
    cwd: str | None = None
    """Working directory the CLI runs in. None = caller's cwd."""

    max_budget_usd: Decimal | None = None
    """Hard cap on spend per invocation. Required when calling Claude Code."""

    timeout_seconds: float = 120.0
    extra_args: list[str] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class DelegationResponse:
    content: str
    """Final agent output (what the CLI produced)."""

    raw_output: str
    """Raw stdout from the subprocess for debugging."""

    is_error: bool = False
    error_message: str | None = None
    duration_ms: int = 0

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Decimal = Decimal("0")

    session_id: str | None = None
    """Some CLIs return a resumable session id."""


class DelegationError(Exception):
    """Generic delegation failure (process exited non-zero, parse failure, etc.)."""


class DelegationTimeout(DelegationError):
    """The subprocess exceeded the timeout."""


class DelegationBudgetExceeded(DelegationError):
    """The CLI reported it hit the configured budget cap before finishing."""

    def __init__(self, message: str, *, cost_usd: Decimal) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd
