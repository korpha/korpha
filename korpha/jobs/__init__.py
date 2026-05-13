"""In-process background job runtime for long-running Codex calls.

Why: ``code.ship_via_codex`` blocks the chat for the full duration
of the Codex run (often 5+ minutes for non-trivial refactors).
With this module, the skill can fire Codex as an asyncio.Task,
return a ``job_id`` immediately, and let the founder ask "is X
done?" or "what happened with X?" via separate skill invocations.

Scope:
  - In-memory ``JobRegistry`` (one per process). DB persistence
    can come later — Mike isn't running 100s of jobs concurrently.
  - Status transitions: ``pending`` → ``running`` → ``completed`` |
    ``failed`` | ``cancelled``.
  - Result captured on completion; readable by ``get_job(id)``.
  - ``cancel(id)`` cooperatively cancels via the asyncio.Task.
  - Auto-prune of completed jobs older than the retention window
    so the registry doesn't grow unbounded across a long uptime.

What's intentionally NOT in scope yet:
  - Cross-process / cross-restart durability (use Activity log
    for "what happened" — the registry is for "what's running
    right now").
  - Push notifications to Telegram / email on completion. Pairs
    naturally with the cross-channel send_message skill from
    commit #152; ship the runtime first, wire the push when
    the first founder asks for it.
"""
from korpha.jobs.registry import (
    DEFAULT_RETENTION_SECONDS,
    Job,
    JobRegistry,
    JobStatus,
    job_registry,
)

__all__ = [
    "DEFAULT_RETENTION_SECONDS",
    "Job",
    "JobRegistry",
    "JobStatus",
    "job_registry",
]
