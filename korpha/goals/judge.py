"""Judge primitive — strict-JSON verdict on whether a goal is done."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_MAX_TURNS = 20
"""Cap on judge-driven continuations per goal. Mike can override per-call.
Hermes uses 20; we keep parity. The cap is the backstop against runaway
loops if the judge can't tell the goal is unachievable."""

DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES = 3
"""After this many consecutive judge-reply parse failures (empty output,
non-JSON, missing keys), auto-pause the loop and tell the founder to
configure a stronger judge model. Doesn't count network / API errors —
those are transient and just retry. From Hermes' lesson learned with weak
small models that can't follow the strict-JSON contract."""

# Cap how much of the agent's last response we send the judge —
# the judge only needs to read a slice to decide done vs continue.
_JUDGE_RESPONSE_SNIPPET_CHARS = 4000


JUDGE_SYSTEM_PROMPT = (
    "You are a strict judge evaluating whether an autonomous agent has "
    "achieved a user's stated goal. You receive the goal text and the "
    "agent's most recent response. Your only job is to decide whether "
    "the goal is fully satisfied based on that response.\n\n"
    "A goal is DONE only when:\n"
    "- The response explicitly confirms the goal was completed, OR\n"
    "- The response clearly shows the final deliverable was produced, OR\n"
    "- The response explains the goal is unachievable / blocked / needs "
    "user input (treat this as DONE with reason describing the block).\n\n"
    "Otherwise the goal is NOT done — CONTINUE.\n\n"
    "Reply ONLY with a single JSON object on one line:\n"
    '{"done": <true|false>, "reason": "<one-sentence rationale>"}'
)


JUDGE_USER_PROMPT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Agent's most recent response:\n{response}\n\n"
    "Is the goal satisfied?"
)


@dataclass(frozen=True)
class JudgeVerdict:
    """Parsed judge response.

    ``parsed`` is False when the judge returned non-JSON / wrong shape /
    empty output. The manager treats those as soft-continue but counts
    them toward the consecutive-parse-failure auto-pause backstop.

    ``done`` is the judge's verdict (only meaningful when parsed=True).
    """

    done: bool
    reason: str
    parsed: bool


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_judge_response(raw: str) -> JudgeVerdict:
    """Extract a verdict from the judge's reply. Tolerant — many small
    models wrap JSON in markdown fences, prepend reasoning, or trail
    apologies. We grab the first JSON object that parses + has a
    boolean ``done`` key."""
    if not raw or not raw.strip():
        return JudgeVerdict(done=False, reason="(empty judge output)", parsed=False)

    # Try the whole thing first; some judges nail it.
    candidates: list[str] = [raw.strip()]
    # Also try the first {...} blob in case there's preamble.
    match = _JSON_OBJECT_RE.search(raw)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if "done" not in data:
            continue
        done_raw = data.get("done")
        if isinstance(done_raw, bool):
            done = done_raw
        elif isinstance(done_raw, str):
            done = done_raw.strip().lower() in ("true", "yes", "done", "1")
        else:
            continue
        reason = str(data.get("reason") or "").strip()
        return JudgeVerdict(
            done=done,
            reason=reason or ("done" if done else "continue"),
            parsed=True,
        )
    return JudgeVerdict(
        done=False,
        reason=f"(judge reply unparseable: {raw[:120]!r})",
        parsed=False,
    )


def truncate_response(text: str, *, limit: int = _JUDGE_RESPONSE_SNIPPET_CHARS) -> str:
    """Cap the agent reply we send the judge — the verdict signal is
    in the first/last few KB; cheap savings on judge tokens.

    Returns text unchanged when under the cap. Otherwise keeps a
    head + tail slice with a ``[truncated]`` marker. Sized so the
    output never exceeds ``limit + len(marker)`` regardless of how
    small ``limit`` is — pre-fix this rounded into negative slice
    indices and produced output longer than the input."""
    if len(text) <= limit:
        return text
    marker = "\n…[truncated]…\n"
    available = max(40, limit - len(marker))
    head_len = max(20, int(available * 0.7))
    tail_len = max(20, available - head_len)
    return f"{text[:head_len]}{marker}{text[-tail_len:]}"


__all__ = [
    "DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES",
    "DEFAULT_MAX_TURNS",
    "JUDGE_SYSTEM_PROMPT",
    "JUDGE_USER_PROMPT_TEMPLATE",
    "JudgeVerdict",
    "parse_judge_response",
    "truncate_response",
]
