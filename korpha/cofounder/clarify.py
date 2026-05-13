"""Structured multi-choice clarifying questions.

Why: Mike is non-technical. When the cofounder needs to clarify
("Should we go with niche A or niche B?", "Want me to draft the
copy first or set up Stripe first?"), free-text replies produce
ambiguous answers ("the first one"? "yeah the second"?). A
structured request — question + up to 4 choices — lets the UI
render clickable options and disambiguates the response.

Architecture:

  - CEO router emits ``action="clarify"`` with question + choices
    instead of a plain ``respond``.
  - ``HandleResult.clarify`` carries the request to the channel.
  - Web dashboard renders choices as HTMX-submitting buttons.
  - TUI renders as a numbered list inline in the chat log.
  - Telegram / messaging channels render as a numbered list (the
    LLM's ``content`` field already has the question phrased
    naturally; choices append at the end).

Inspired by Hermes' ``tools/clarify_tool.py``. Adapted to fit
Korpha's CEO router (where intent emission goes through
``_RouterDecision`` rather than tool-call schemas).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Cap matches Hermes — beyond 4 choices the UI gets cluttered and
# the LLM is usually inventing options anyway.
MAX_CHOICES = 4


@dataclass(frozen=True)
class ClarifyRequest:
    """A structured clarify question the cofounder is asking the founder."""

    question: str
    choices: tuple[str, ...] = field(default_factory=tuple)
    """Up to 4 predefined choices. Empty tuple → open-ended question
    (just render the text, no buttons)."""

    def is_open_ended(self) -> bool:
        return not self.choices

    def as_numbered_list(self) -> str:
        """Render choices as a numbered list — used by channels that
        can't render clickable buttons (Telegram, plain-text email).
        Returns empty string when open-ended."""
        if self.is_open_ended():
            return ""
        return "\n".join(
            f"{i + 1}. {choice}"
            for i, choice in enumerate(self.choices)
        )


def parse_clarify(raw: dict) -> ClarifyRequest | None:
    """Parse a clarify intent from a router-decision dict. Returns
    None when the shape is wrong — caller falls back to plain
    respond."""
    question = str(raw.get("question") or raw.get("content") or "").strip()
    if not question:
        return None
    raw_choices = raw.get("choices") or []
    choices: list[str] = []
    if isinstance(raw_choices, list):
        for c in raw_choices:
            text = str(c).strip()
            if text:
                choices.append(text)
            if len(choices) >= MAX_CHOICES:
                break
    return ClarifyRequest(question=question, choices=tuple(choices))


__all__ = ["MAX_CHOICES", "ClarifyRequest", "parse_clarify"]
