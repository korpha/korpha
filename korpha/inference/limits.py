"""Centralized agent limits — max_tokens, timeouts, retry counts.

Every default that controls how generous Korpha is with the LLM
lives here. Floors are sized for **reasoning models** (DeepSeek V4 Pro,
Kimi K2.6, GLM-5, Qwen3, Claude with extended thinking, OpenAI o-series,
Gemini thinking) — these models burn chain-of-thought tokens before the
visible answer, so a tight cap returns empty content.

Why a single module:

  - Open-source code shouldn't hardcode magic numbers users may want
    to tune. A non-technical Founder can override any of these in
    ``providers.yaml`` under the ``defaults:`` section without touching
    Python.
  - Keeps the floors honest. Anyone bumping a value reads the
    docstring in this file and sees *why* the floor exists.

Override format in ``providers.yaml``:

```yaml
defaults:
  max_tokens_normal: 16000      # CEO, Director, Worker, skills
  max_tokens_coding: 128000     # coding loops (when Korpha dispatches code work)
  agent_timeout_seconds: 300    # 5 min — generous for reasoning models
  request_timeout_seconds: 60   # HTTP connect + read for non-LLM API calls
```

Or override a single field via env var (handy for CI):

  KORPHA_MAX_TOKENS_NORMAL=8000

"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Floors — DO NOT lower without reading feedback_max_tokens_floors.md
# ---------------------------------------------------------------------------

DEFAULT_MAX_TOKENS_NORMAL: int = 16_000
"""Floor for any agent call that isn't a coding loop. Reasoning models
spend 2-8k on chain-of-thought before producing visible output. 16k
leaves enough headroom for thinking + a meaningful answer for plans,
analyses, drafts, summaries, triage replies, and skill outputs."""

DEFAULT_MAX_TOKENS_CODING: int = 128_000
"""Floor for coding loops — when Korpha dispatches code-write /
refactor / review work to a coding agent. Modern coding tools (Codex
CLI, Claude Code, Aider) run multi-step loops with substantial context
and large diffs. 128k matches what frontier models can actually emit."""

DEFAULT_AGENT_TIMEOUT_SECONDS: float = 300.0
"""Wall-clock budget for one LLM completion. Reasoning models can take
60-120s end-to-end; 300s leaves headroom for retries within a single
call. Override per-call when a skill genuinely needs more (research
sweeps) or less (quick triage)."""

DEFAULT_REQUEST_TIMEOUT_SECONDS: float = 60.0
"""HTTP connect + read for non-LLM API calls (Stripe, Resend,
RankMyAnswer, MCP server). Distinct from LLM completion timeouts
because these are deterministic API calls, not generative."""


# ---------------------------------------------------------------------------
# Loader — reads providers.yaml ``defaults:`` once, caches at module level
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentLimits:
    """Resolved limits — what every caller actually uses at runtime."""

    max_tokens_normal: int
    max_tokens_coding: int
    agent_timeout_seconds: float
    request_timeout_seconds: float


_CACHED: AgentLimits | None = None


def get_limits() -> AgentLimits:
    """Return resolved limits (yaml override > env var > floor)."""
    global _CACHED
    if _CACHED is not None:
        return _CACHED
    _CACHED = _resolve()
    return _CACHED


def reset_cache() -> None:
    """Force re-read on next ``get_limits()``. Tests use this to swap
    in a per-test override without touching the global config file."""
    global _CACHED
    _CACHED = None


def _resolve() -> AgentLimits:
    yaml_defaults = _read_yaml_defaults()

    return AgentLimits(
        max_tokens_normal=_int_from(
            "KORPHA_MAX_TOKENS_NORMAL",
            yaml_defaults.get("max_tokens_normal"),
            DEFAULT_MAX_TOKENS_NORMAL,
        ),
        max_tokens_coding=_int_from(
            "KORPHA_MAX_TOKENS_CODING",
            yaml_defaults.get("max_tokens_coding"),
            DEFAULT_MAX_TOKENS_CODING,
        ),
        agent_timeout_seconds=_float_from(
            "KORPHA_AGENT_TIMEOUT_SECONDS",
            yaml_defaults.get("agent_timeout_seconds"),
            DEFAULT_AGENT_TIMEOUT_SECONDS,
        ),
        request_timeout_seconds=_float_from(
            "KORPHA_REQUEST_TIMEOUT_SECONDS",
            yaml_defaults.get("request_timeout_seconds"),
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
    )


def _read_yaml_defaults() -> dict[str, Any]:
    """Read ``defaults:`` mapping from providers.yaml. Returns empty
    dict on any failure — limits should always have safe fallbacks."""
    override = os.getenv("KORPHA_PROVIDERS_FILE")
    p = Path(override).expanduser() if override else (
        Path.home() / ".korpha" / "providers.yaml"
    )
    if not p.exists():
        return {}
    try:
        import yaml

        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    defaults = body.get("defaults")
    return defaults if isinstance(defaults, dict) else {}


def _int_from(env_name: str, yaml_value: Any, floor: int) -> int:
    raw = os.getenv(env_name)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    if yaml_value is not None:
        try:
            return int(yaml_value)
        except (ValueError, TypeError):
            pass
    return floor


def _float_from(env_name: str, yaml_value: Any, floor: float) -> float:
    raw = os.getenv(env_name)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    if yaml_value is not None:
        try:
            return float(yaml_value)
        except (ValueError, TypeError):
            pass
    return floor


# ---------------------------------------------------------------------------
# Convenience accessors — one-liners for the common case
# ---------------------------------------------------------------------------


def agent_max_tokens() -> int:
    """``max_tokens`` for normal agent calls (CEO, Director, Worker, skills)."""
    return get_limits().max_tokens_normal


def coding_max_tokens() -> int:
    """``max_tokens`` for coding loops (Codex CLI / Claude Code dispatched work)."""
    return get_limits().max_tokens_coding


def agent_timeout() -> float:
    """Wall-clock budget for one LLM completion."""
    return get_limits().agent_timeout_seconds


def request_timeout() -> float:
    """HTTP connect + read for non-LLM API calls."""
    return get_limits().request_timeout_seconds


__all__ = [
    "DEFAULT_AGENT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_TOKENS_CODING",
    "DEFAULT_MAX_TOKENS_NORMAL",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "AgentLimits",
    "agent_max_tokens",
    "agent_timeout",
    "coding_max_tokens",
    "get_limits",
    "request_timeout",
    "reset_cache",
]
