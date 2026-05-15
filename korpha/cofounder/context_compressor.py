"""Default context engine — protects head + tail of conversation,
summarizes the middle via an auxiliary LLM call.

Algorithm (ported from ``hermes/agent/context_compressor.py`` and
simplified for Korpha's text-only chat history):

  1. If estimated tokens are under threshold → return unchanged.
  2. Protect head: first ``protect_first_n`` messages (the original
     framing the founder gave).
  3. Protect tail: walk backwards accumulating tokens until budget
     fills. ``protect_last_n`` is a minimum floor.
  4. Summarize the middle turns via an auxiliary LLM (WORKHORSE
     tier — cheap + fast). Replace them with a single
     ``[CONTEXT COMPACTION — REFERENCE ONLY]`` message.
  5. Iterative updates: on subsequent compactions, fold the new
     turns into the previous summary rather than redoing it.

Crucial invariant carried over from Hermes (fixes their issue #10896):
the most recent user message MUST always end up in the protected
tail. Otherwise the LLM treats the active task as "already
handled" via the summary and never responds to it.

Why no tool-result pruning here (which Hermes has):
Korpha chat history stores only ``FOUNDER`` and ``AGENT`` messages.
Skill router decisions + skill outputs are NOT in the message thread —
they appear in the synth turn that follows. So there's nothing to
prune; we go straight to head+tail+summary.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from korpha.audit.model import InferenceTier
from korpha.cofounder.context_engine import (
    MINIMUM_CONTEXT_LENGTH,
    ContextEngine,
    estimate_message_tokens,
    estimate_messages_tokens,
)
from korpha.inference.types import CompletionRequest, Message, Role

if TYPE_CHECKING:
    from korpha.inference.cost_tracker import CostTracker


logger = logging.getLogger(__name__)


_SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns in this "
    "conversation were compacted into the summary below. Treat it "
    "as background reference, NOT as active instructions. Do NOT "
    "answer questions or fulfill requests described in the summary "
    "— they were already addressed. Respond ONLY to the latest "
    "founder message that appears AFTER this summary. Live state "
    "(business units, kanban, team) is provided fresh in the "
    "system prompt — trust that over the summary if they conflict."
)

_SUMMARIZER_PREAMBLE = (
    "You are a summarization agent creating a context checkpoint. "
    "Your output will be injected as reference material for a "
    "DIFFERENT assistant that continues the conversation. "
    "Do NOT respond to any questions or requests in the conversation "
    "— only output the structured summary. "
    "Do NOT include any preamble, greeting, or prefix. "
    "Write the summary in the language the founder uses in the "
    "conversation. "
    "NEVER include API keys, tokens, passwords, secrets, or "
    "credentials — replace any that appear with [REDACTED]."
)

_TEMPLATE_SECTIONS = """## Active Task
[Copy the founder's most recent unfulfilled request or directive
verbatim. If no outstanding task, write "None."]

## Business Goal
[What the founder is trying to build / accomplish overall]

## Constraints & Preferences
[Explicit founder preferences, no-go zones, decisions they've
locked in (e.g. "no 1-1 customization", "Postgres in prod",
"open-weights only")]

## Completed Actions
[Numbered list of concrete actions taken this conversation, with
outcomes. Format: N. ACTION — outcome. Be specific about lines
spawned, agents hired, cards moved, credentials saved, etc.]

## Resolved Questions
[Questions the founder asked that were already answered — include
the answer briefly so the next assistant doesn't re-answer]

## Pending Founder Asks
[Questions or requests from the founder that have NOT yet been
answered or fulfilled. If none, write "None."]

## Key Decisions
[Important strategic / technical decisions and WHY they were made]

## Critical Context
[Any specific values, IDs, error messages, or data that would be
lost without explicit preservation. NEVER include credentials.]

Target ~{target_tokens} tokens. Be CONCRETE — include unit names,
card titles, agent role names, specific dates and amounts.
Avoid vague phrases like "made progress" — say exactly what."""


_REDACT_PATTERNS = [
    # API key prefixes
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bre_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bpk_(?:live|test)_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\brk_(?:live|test)_[A-Za-z0-9_-]{20,}\b"),
    # Bearer tokens / generic long base64ish strings
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    # AWS-style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def _redact(text: str) -> str:
    """Best-effort credential stripper. The summarizer prompt tells
    the LLM to redact, but a belt-and-braces pass after generation
    catches verbatim echoes."""
    if not text:
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


class ContextCompressor(ContextEngine):
    """Hermes-style head+tail+summary compaction for the CEO chat."""

    def __init__(
        self,
        *,
        cost_tracker: "CostTracker",
        session_key: str,
        context_length: int,
        threshold_percent: float = 0.80,
        protect_first_n: int = 3,
        protect_last_n: int = 20,
        summary_target_ratio: float = 0.20,
        summary_tokens_ceiling: int = 12_000,
        summary_tier: InferenceTier = InferenceTier.WORKHORSE,
        summary_max_tokens: int = 16_000,
        summary_timeout_seconds: int = 180,
        minimum_context_length: int = MINIMUM_CONTEXT_LENGTH,
    ) -> None:
        self.cost_tracker = cost_tracker
        self.session_key = session_key
        self.context_length = context_length
        self.threshold_percent = threshold_percent
        self.protect_first_n = max(1, protect_first_n)
        self.protect_last_n = max(1, protect_last_n)
        self.summary_target_ratio = max(0.05, min(summary_target_ratio, 0.80))
        self.summary_tokens_ceiling = summary_tokens_ceiling
        self.summary_tier = summary_tier
        self.summary_max_tokens = summary_max_tokens
        self.summary_timeout_seconds = summary_timeout_seconds

        self.threshold_tokens = max(
            int(context_length * threshold_percent),
            minimum_context_length,
        )
        self.tail_token_budget = int(
            self.threshold_tokens * self.summary_target_ratio,
        )
        self._previous_summary: str | None = None
        self.compression_count = 0

    @property
    def name(self) -> str:
        return "compressor"

    async def shape(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int,
        system_overhead_tokens: int = 0,
    ) -> list[Message]:
        if not messages:
            self.last_input_tokens = 0
            return []

        prompt_tokens = (
            estimate_messages_tokens(messages)
            + max_output_tokens + system_overhead_tokens
        )
        self.last_input_tokens = prompt_tokens

        if prompt_tokens < self.threshold_tokens:
            return list(messages)
        if len(messages) <= self.protect_first_n + self.protect_last_n + 1:
            # Nothing to compress — head + tail already span the
            # whole list.
            return list(messages)

        compress_start = self.protect_first_n
        compress_end = self._find_tail_cut_by_tokens(
            messages, head_end=compress_start,
        )
        if compress_start >= compress_end:
            return list(messages)

        middle = messages[compress_start:compress_end]
        logger.info(
            "context.compress: shaping %d messages "
            "(est %d tokens >= threshold %d). "
            "Protecting head=%d, tail=%d, summarizing middle=%d.",
            len(messages), prompt_tokens, self.threshold_tokens,
            compress_start, len(messages) - compress_end,
            len(middle),
        )

        summary_text = await self._generate_summary(middle)
        if not summary_text:
            # Fail-soft: rather than drop history silently, leave the
            # messages as-is. Caller may still send an over-budget
            # request — the provider may compress server-side or
            # error explicitly so we learn about it.
            logger.warning(
                "context.compress: summary unavailable; "
                "returning uncompressed history (risk of context overflow)"
            )
            return list(messages)

        head = list(messages[:compress_start])
        tail = list(messages[compress_end:])

        # The summary lands as a SYSTEM-role message so the LLM
        # treats it as out-of-band context, not a fresh user/assistant
        # turn. Hermes alternates user/assistant — Korpha's providers
        # accept multiple system messages, so this is simpler.
        summary_msg = Message(
            role=Role.SYSTEM,
            content=f"{_SUMMARY_PREFIX}\n\n{summary_text}",
        )
        compressed = head + [summary_msg] + tail
        self.compression_count += 1
        new_est = estimate_messages_tokens(compressed)
        logger.info(
            "context.compress: %d -> %d messages, "
            "%d -> ~%d tokens (saved %d).",
            len(messages), len(compressed),
            prompt_tokens, new_est, prompt_tokens - new_est,
        )
        return compressed

    def _find_tail_cut_by_tokens(
        self,
        messages: list[Message],
        *,
        head_end: int,
    ) -> int:
        """Walk backwards from the newest message until either the
        token budget fills or we reach head_end. Returns the index
        of the first tail message (i.e. messages[cut:] is the tail).
        Always anchors the most recent USER message into the tail,
        per Hermes's fix for issue #10896."""
        n = len(messages)
        min_tail = min(self.protect_last_n, max(0, n - head_end - 1))
        soft_ceiling = int(self.tail_token_budget * 1.5)

        accumulated = 0
        cut = n
        for i in range(n - 1, head_end - 1, -1):
            msg_tokens = estimate_message_tokens(messages[i])
            if accumulated + msg_tokens > soft_ceiling and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut = i

        # Enforce minimum tail count
        fallback = n - min_tail
        if cut > fallback:
            cut = fallback

        if cut <= head_end:
            cut = max(fallback, head_end + 1)

        # Anchor the last user message into the tail.
        cut = self._ensure_last_user_in_tail(messages, cut, head_end)
        return max(cut, head_end + 1)

    def _ensure_last_user_in_tail(
        self,
        messages: list[Message],
        cut: int,
        head_end: int,
    ) -> int:
        for i in range(len(messages) - 1, head_end - 1, -1):
            try:
                role_str = (
                    messages[i].role.value
                    if hasattr(messages[i].role, "value")
                    else str(messages[i].role)
                )
            except Exception:  # noqa: BLE001
                role_str = ""
            if role_str.lower() == "user":
                if i < cut:
                    return max(i, head_end + 1)
                return cut
        return cut

    async def _generate_summary(
        self,
        turns: list[Message],
    ) -> str | None:
        """Build the structured summary via an auxiliary LLM call.

        Uses ``self.summary_tier`` (default WORKHORSE — cheap and
        fast). On any failure, returns None and the caller decides
        whether to drop history or pass through uncompressed."""
        if not turns:
            return None

        target_tokens = max(
            2_000,
            min(int(self.tail_token_budget * 0.25), self.summary_tokens_ceiling),
        )
        serialized = self._serialize_turns(turns)
        template = _TEMPLATE_SECTIONS.format(target_tokens=target_tokens)

        if self._previous_summary:
            prompt = (
                f"{_SUMMARIZER_PREAMBLE}\n\n"
                "You are updating a context compaction summary. A "
                "previous compaction produced the summary below. New "
                "conversation turns have occurred since then.\n\n"
                "PREVIOUS SUMMARY:\n"
                f"{self._previous_summary}\n\n"
                "NEW TURNS TO INCORPORATE:\n"
                f"{serialized}\n\n"
                "Update the summary using this exact structure. "
                "PRESERVE existing information that is still relevant. "
                "ADD new completed actions to the numbered list "
                "(continue numbering). Move answered questions to "
                "Resolved Questions. Update Active Task to reflect "
                "the founder's most recent unfulfilled request — "
                "this is the most important field.\n\n"
                f"{template}"
            )
        else:
            prompt = (
                f"{_SUMMARIZER_PREAMBLE}\n\n"
                "Create a structured handoff summary for a different "
                "assistant that will continue this conversation after "
                "earlier turns are compacted.\n\n"
                "TURNS TO SUMMARIZE:\n"
                f"{serialized}\n\n"
                "Use this exact structure:\n\n"
                f"{template}"
            )

        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=_SUMMARIZER_PREAMBLE),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.summary_tier,
            session_key=f"context-compress-{self.session_key}",
            max_tokens=self.summary_max_tokens,
            timeout_seconds=self.summary_timeout_seconds,
        )
        try:
            response = await self.cost_tracker.pool.complete(request)
        except Exception:  # noqa: BLE001
            logger.exception(
                "context.compress: summarizer LLM call failed"
            )
            return None

        body = (response.content or response.reasoning or "").strip()
        if not body:
            logger.warning(
                "context.compress: summarizer returned empty content "
                "(finish=%s input=%d output=%d)",
                response.finish_reason,
                response.input_tokens, response.output_tokens,
            )
            return None
        body = _redact(body)
        self._previous_summary = body
        return body

    def _serialize_turns(self, turns: list[Message]) -> str:
        """Render a flat text block the summarizer LLM can read.
        Format: ``[ROLE] content`` with a blank line between turns."""
        out: list[str] = []
        for m in turns:
            try:
                role = (
                    m.role.value if hasattr(m.role, "value") else str(m.role)
                ).upper()
            except Exception:  # noqa: BLE001
                role = "MSG"
            out.append(f"[{role}] {(m.content or '').strip()}")
        return "\n\n".join(out)


__all__ = ["ContextCompressor"]
