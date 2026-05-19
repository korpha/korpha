"""Shared ``/goal`` slash-command parser + dispatcher.

Used by every chat surface that wants to handle ``/goal`` inline
without round-tripping through the agent loop:

  - TUI: ``korpha/tui/app.py`` slash dispatcher
  - Dashboard chat: ``/ask/stream`` interception in ``korpha/api/server.py``
  - Gateway adapters (future): same call shape

The parser returns a structured intent so each surface can render its
own confirmation text style (rich markup in the TUI, plain text in
chat). The executor mutates the GoalManager + returns a one-line
human-friendly reply string suitable for any surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from korpha.goals.manager import GoalManager


GoalAction = Literal["status", "set", "pause", "resume", "clear", "help", "unknown"]


@dataclass(frozen=True)
class GoalSlashIntent:
    """Structured ``/goal ...`` parse result."""

    action: GoalAction
    text: str = ""
    """Goal text — only set when action == "set"."""

    force: bool = False
    """When action == "set", whether to replace an existing active goal."""

    raw: str = ""
    """The original ``/goal ...`` line, for error messages."""


def is_goal_slash(text: str) -> bool:
    """True if the message line is a ``/goal`` slash invocation —
    including bare ``/goal`` (= status alias)."""
    stripped = text.strip()
    if not stripped.startswith("/goal"):
        return False
    # Reject things like "/goalkeeper" — only accept /goal followed
    # by end-of-string or whitespace.
    after = stripped[len("/goal"):]
    return after == "" or after[:1].isspace()


def parse_goal_slash(text: str) -> GoalSlashIntent:
    """Parse ``/goal ...`` into an intent.

    Forms:
      /goal                — alias for /goal status (Hermes parity)
      /goal status         — show current goal state
      /goal pause          — pause continuation loop
      /goal resume         — resume from paused state (resets counter)
      /goal clear          — drop the active goal entirely
      /goal help           — usage one-liner
      /goal --force <txt>  — set a goal, replacing any active one
      /goal <text>         — set a goal (refuses if active exists)
    """
    if not is_goal_slash(text):
        return GoalSlashIntent(action="unknown", raw=text)

    body = text.strip()[len("/goal"):].strip()
    if not body:
        return GoalSlashIntent(action="status", raw=text)

    head, _, rest = body.partition(" ")
    head_low = head.lower()

    if head_low == "status":
        return GoalSlashIntent(action="status", raw=text)
    if head_low == "pause":
        return GoalSlashIntent(action="pause", raw=text)
    if head_low == "resume":
        return GoalSlashIntent(action="resume", raw=text)
    if head_low == "clear":
        return GoalSlashIntent(action="clear", raw=text)
    if head_low == "help":
        return GoalSlashIntent(action="help", raw=text)

    # Set (with or without --force prefix)
    if head_low == "--force":
        return GoalSlashIntent(
            action="set", text=rest.strip(), force=True, raw=text,
        )
    return GoalSlashIntent(action="set", text=body, force=False, raw=text)


_HELP_TEXT = (
    "/goal <text>          set a standing goal (run until done)\n"
    "/goal --force <text>  replace the active goal (mid-run safe)\n"
    "/goal status (or bare /goal)  show the current goal + turn count\n"
    "/goal pause           stop the continuation loop, keep the goal\n"
    "/goal resume          resume from pause (resets turn budget)\n"
    "/goal clear           drop the goal entirely"
)


def execute_goal_slash(
    intent: GoalSlashIntent,
    manager: "GoalManager",
) -> str:
    """Dispatch the parsed intent against a GoalManager. Returns the
    one-line (or multi-line for status/help) reply string.

    Doesn't raise — every error path returns a friendly string. The
    caller surfaces it to the user as-is.
    """
    from korpha.goals.manager import GoalReplaceConflict
    from korpha.goals.model import GoalStatus

    if intent.action == "unknown":
        return f"Not a /goal command: {intent.raw!r}"
    if intent.action == "help":
        return _HELP_TEXT

    if intent.action == "status":
        goal = manager.active() or manager.latest()
        if goal is None:
            return "(no goal set — run `/goal <text>` to start one)"
        used = f"{goal.turns_used}/{goal.max_turns}"
        if goal.status == GoalStatus.ACTIVE:
            return (
                f"⊙ Active goal ({used} turns used): {goal.text}"
            )
        if goal.status == GoalStatus.PAUSED:
            return (
                f"⏸ Paused goal ({used} turns used): {goal.text} — "
                f"use `/goal resume` to continue."
            )
        if goal.status == GoalStatus.ACHIEVED:
            return (
                f"✓ Last goal achieved ({used} turns): {goal.text}"
            )
        return f"{goal.status.value.title()} goal: {goal.text}"

    if intent.action == "set":
        if not intent.text:
            return "Goal text is required. Try `/goal <what you want done>`."
        try:
            goal = manager.set(intent.text, force=intent.force)
        except GoalReplaceConflict as exc:
            return str(exc)
        except ValueError as exc:
            return f"Can't set goal: {exc}"
        return (
            f"⊙ Goal set ({goal.max_turns}-turn budget): {goal.text}"
        )

    if intent.action == "pause":
        goal = manager.pause(reason="user-paused")
        if goal is None:
            return "(no active goal to pause)"
        return f"⏸ Paused goal: {goal.text}"

    if intent.action == "resume":
        goal = manager.resume(reset_budget=True)
        if goal is None:
            return "(no paused goal to resume)"
        return (
            f"↻ Resumed goal (0/{goal.max_turns} turns used): {goal.text}"
        )

    if intent.action == "clear":
        goal = manager.clear()
        if goal is None:
            return "(no goal to clear)"
        return f"✗ Cleared goal: {goal.text}"

    return f"Unknown /goal action: {intent.action!r}"


__all__ = [
    "GoalAction",
    "GoalSlashIntent",
    "execute_goal_slash",
    "is_goal_slash",
    "parse_goal_slash",
]
