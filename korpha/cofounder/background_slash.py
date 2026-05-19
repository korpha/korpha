"""``/background`` slash command parser + dispatcher.

Mirrors the ``/goal`` shape (korpha/goals/slash.py). One parser used
by every chat surface (TUI, dashboard chat, future gateways).

Forms:
  /background list                       — show active + recent
  /background status <job_id>            — detail on one task
  /background cancel <job_id>            — cancel running/pending
  /background <free-text task>           — spawn new task
  /background help                       — usage one-liner
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


BackgroundAction = Literal[
    "list", "status", "cancel", "spawn", "help", "unknown",
]


@dataclass(frozen=True)
class BackgroundSlashIntent:
    """Structured ``/background ...`` parse result."""

    action: BackgroundAction
    text: str = ""
    """Task description — set when action == 'spawn'."""

    job_id: str = ""
    """Target job id — set when action in ('status', 'cancel')."""

    raw: str = ""
    """The original /background ... line, for error messages."""


def is_background_slash(text: str) -> bool:
    """True if the message line is a ``/background`` invocation —
    including bare ``/background`` (= list alias)."""
    stripped = text.strip()
    if not stripped.startswith("/background"):
        return False
    after = stripped[len("/background"):]
    return after == "" or after[:1].isspace()


def parse_background_slash(text: str) -> BackgroundSlashIntent:
    """Parse ``/background ...`` into an intent."""
    if not is_background_slash(text):
        return BackgroundSlashIntent(action="unknown", raw=text)

    body = text.strip()[len("/background"):].strip()
    if not body:
        return BackgroundSlashIntent(action="list", raw=text)

    head, _, rest = body.partition(" ")
    head_low = head.lower()

    if head_low == "list":
        return BackgroundSlashIntent(action="list", raw=text)
    if head_low == "help":
        return BackgroundSlashIntent(action="help", raw=text)
    if head_low == "status":
        return BackgroundSlashIntent(
            action="status", job_id=rest.strip(), raw=text,
        )
    if head_low == "cancel":
        return BackgroundSlashIntent(
            action="cancel", job_id=rest.strip(), raw=text,
        )

    # Anything else → spawn new task with the entire body as the prompt
    return BackgroundSlashIntent(action="spawn", text=body, raw=text)


_HELP_TEXT = (
    "/background <task>                 spawn a new background task\n"
    "/background list (or bare /background)  show active + recent\n"
    "/background status <job_id>        detail on one task\n"
    "/background cancel <job_id>        cancel a running/pending task"
)


def execute_background_slash_listing(
    intent: BackgroundSlashIntent, *, business_id=None,
) -> str:
    """Handle list / status / cancel — no agent turn needed.

    Spawn lives in execute_background_slash_spawn (which needs CEO
    + router + Founder + Business + thread) so the surface code can
    construct it.
    """
    from korpha.cofounder.background_tasks import (
        cancel_background_task, list_active_jobs, list_recent_jobs,
    )
    from korpha.jobs.registry import job_registry, JobStatus

    if intent.action == "list":
        active = list_active_jobs(business_id=business_id)
        recent = [
            j for j in list_recent_jobs(business_id=business_id, limit=10)
            if j.status in (
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED,
            )
        ]
        if not active and not recent:
            return "(no background tasks — try `/background <task>`)"
        out: list[str] = []
        if active:
            out.append("Active:")
            for j in active:
                out.append(f"  {j.id}  {j.status.value:<8}  {j.label}")
        if recent:
            out.append("Recent:")
            for j in recent[:5]:
                marker = {
                    JobStatus.COMPLETED: "✓",
                    JobStatus.FAILED: "✗",
                    JobStatus.CANCELLED: "⊘",
                }.get(j.status, "?")
                out.append(f"  {marker} {j.id}  {j.label}")
        return "\n".join(out)

    if intent.action == "status":
        if not intent.job_id:
            return "Usage: /background status <job_id>"
        job = job_registry.get(intent.job_id)
        if job is None:
            return f"No background task with id {intent.job_id!r}"
        lines = [
            f"id:       {job.id}",
            f"label:    {job.label}",
            f"status:   {job.status.value}",
        ]
        if job.duration_seconds() is not None:
            lines.append(f"duration: {job.duration_seconds():.1f}s")
        if job.error:
            lines.append(f"error:    {job.error}")
        if (
            isinstance(job.result, dict)
            and "content" in job.result
        ):
            preview = str(job.result["content"])[:200]
            if len(str(job.result["content"])) > 200:
                preview += "…"
            lines.append(f"output:   {preview}")
        return "\n".join(lines)

    if intent.action == "cancel":
        if not intent.job_id:
            return "Usage: /background cancel <job_id>"
        if cancel_background_task(intent.job_id):
            return f"⊘ Cancelled background task {intent.job_id}"
        return f"Couldn't cancel {intent.job_id!r} (not found or already done)"

    if intent.action == "help":
        return _HELP_TEXT

    return f"Unknown /background action: {intent.action!r}"


__all__ = [
    "BackgroundAction",
    "BackgroundSlashIntent",
    "execute_background_slash_listing",
    "is_background_slash",
    "parse_background_slash",
]
