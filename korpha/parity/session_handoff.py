"""``/handoff`` — move an active session to a different model/persona.

Use case: agent's mid-task, the user realizes they want Claude Opus
instead of Sonnet for this part. ``/handoff claude-opus`` keeps the
full conversation history + tool-call state and resumes on the new
model. The model name maps through the proxy aliases for the
subscription-OAuth case, or through the regular inference cascade
for paid-API providers.

Doesn't restart the loop — the next turn just runs against the new
provider with the same message history. Cheap. The win is muscle-
memory ("I always want Opus for this step") without losing context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandoffResult:
    """What changed after the handoff was applied."""

    previous_alias: str
    new_alias: str
    notes: str = ""
    """Any caveats the agent should surface — e.g. "tool calls in
    flight; will resume on next user turn"."""


@dataclass
class SessionHandoff:
    """Per-session record of which model/alias the agent is currently
    speaking through. Lives on the Thread / Session object; updated
    by ``/handoff``.

    Stored as a string alias rather than a ProviderAccount instance
    so persistence is trivial (just a column on Thread)."""

    current_alias: str
    """Active model alias — looked up via korpha.proxy.aliases or the
    inference cascade depending on the routing layer."""

    history: list[str] = None  # type: ignore[assignment]
    """Each entry is a prior alias the session ran under, oldest →
    newest. Lets the dashboard render the model-switch trail."""

    def __post_init__(self):
        if self.history is None:
            self.history = []

    def handoff_to(self, new_alias: str, *, note: str = "") -> HandoffResult:
        """Switch the active alias. Records the old one in history.
        Idempotent on the same alias — no-op return for re-asks of the
        same model."""
        new_alias = (new_alias or "").strip()
        if not new_alias:
            raise ValueError("new_alias must be non-empty")
        if new_alias == self.current_alias:
            return HandoffResult(
                previous_alias=self.current_alias,
                new_alias=new_alias,
                notes="no-op (same alias)",
            )
        previous = self.current_alias
        self.history.append(previous)
        self.current_alias = new_alias
        return HandoffResult(
            previous_alias=previous,
            new_alias=new_alias,
            notes=note,
        )


__all__ = ["HandoffResult", "SessionHandoff"]
