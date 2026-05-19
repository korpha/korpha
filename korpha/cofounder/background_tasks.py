"""Background-task service — agent does work async while user keeps chatting.

Hermes-parity for the ``/background <text>`` ergonomic. Mike queues
research / draft / multi-step tasks, keeps chatting normally, and
results post back into the thread when each one finishes.

Built on the existing ``JobRegistry`` (which already handles status
transitions + on-complete callbacks) and ``ConversationRouter.route_outbound``
for posting completion notices into the channel that originated the
request.

Distinct from:
  - autonomy daemon (continuous goal-driven loop)
  - Codex jobs (vibe-coding subprocesses)
  - /goal (auto-continuation until judge says done)

Background tasks are *named, one-shot, user-initiated* turns — "go
research X, come back when done." When complete, the agent's reply
appears in the founder's chat as a normal outbound message tagged
with the job id.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from korpha.jobs.registry import Job, JobStatus, job_registry

if TYPE_CHECKING:
    from korpha.business.model import Business
    from korpha.cofounder.ceo import CEO
    from korpha.cofounder.routing import ConversationRouter
    from korpha.identity.model import Founder

logger = logging.getLogger(__name__)


# Label prefix so we can distinguish background-task jobs from Codex
# / other future job types when listing.
_LABEL_PREFIX = "background:"


@dataclass
class BackgroundTaskSpec:
    """Inputs for spawning a background task. Mirrors a CEO turn
    minus the SSE shape — we want one synchronous handle() call
    that yields a final response, then deliver it via the channel
    router."""

    task_text: str
    business: "Business"
    founder: "Founder"
    thread_id: UUID
    ceo: "CEO"
    router: "ConversationRouter"
    platform: str = "web"
    """Platform name string for the outbound post. The original
    request's platform — e.g. 'web', 'telegram', 'cli'."""


def spawn_background_task(spec: BackgroundTaskSpec) -> Job:
    """Submit a background task to the JobRegistry. Returns the Job
    snapshot — caller surfaces the job_id to the founder so they
    can later check status with `/background status <id>` or via
    the dashboard.

    When the task finishes (success OR failure), an on-complete
    hook posts the result back into the thread via route_outbound
    so it shows up in the founder's normal chat history alongside
    everything else.
    """
    label = f"{_LABEL_PREFIX} {spec.task_text[:80].strip()}"
    if len(spec.task_text) > 80:
        label += "…"

    async def _work():
        # The actual agent turn. Return the response so JobRegistry
        # stores it as the job result.
        response = await spec.ceo.handle(
            business=spec.business,
            founder=spec.founder,
            founder_message=spec.task_text,
            history=[],
            thread_id=spec.thread_id,
        )
        return {
            "content": response.content or "(no output)",
            "cost_usd": (
                float(response.cost_usd)
                if hasattr(response, "cost_usd") else 0.0
            ),
        }

    async def _on_complete(job: Job) -> None:
        """Post the completion notice into the originating thread.

        Runs even on failure / cancellation so the founder gets a
        clear signal in chat — silent failures of background work
        are worse than visible ones.
        """
        from korpha.cofounder.model import ThreadPlatform
        try:
            platform_enum = ThreadPlatform(spec.platform)
        except ValueError:
            platform_enum = ThreadPlatform.WEB

        if job.status == JobStatus.COMPLETED and isinstance(job.result, dict):
            body = job.result.get("content", "(no output)")
            content = f"[✓ background task {job.id} complete]\n\n{body}"
        elif job.status == JobStatus.CANCELLED:
            content = f"[⊘ background task {job.id} cancelled]"
        else:
            err = job.error or "unknown error"
            content = f"[✗ background task {job.id} failed]\n\n{err}"

        try:
            spec.router.route_outbound(
                business_id=spec.business.id,
                founder_id=spec.founder.id,
                platform=platform_enum,
                content=content,
                requesting_agent_role_id=None,
            )
        except Exception:
            logger.exception(
                "background task %s: outbound post failed; result "
                "preserved in JobRegistry only", job.id,
            )

    return job_registry.submit(
        _work(),
        label=label,
        business_id=str(spec.business.id),
        extra={
            "task_text": spec.task_text,
            "thread_id": str(spec.thread_id),
            "platform": spec.platform,
        },
        on_complete=_on_complete,
    )


def list_active_jobs(business_id: UUID | None = None) -> list[Job]:
    """All non-terminal background jobs, optionally scoped to a
    business. Returns snapshots so callers can pass to UI code
    without worrying about mutation."""
    out: list[Job] = []
    for job in job_registry.list():
        if not job.label.startswith(_LABEL_PREFIX):
            continue
        if business_id is not None and job.business_id != str(business_id):
            continue
        if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
            out.append(job.snapshot())
    return out


def list_recent_jobs(
    business_id: UUID | None = None, *, limit: int = 20,
) -> list[Job]:
    """All background jobs for this business, newest first."""
    out: list[Job] = []
    for job in job_registry.list():
        if not job.label.startswith(_LABEL_PREFIX):
            continue
        if business_id is not None and job.business_id != str(business_id):
            continue
        out.append(job.snapshot())
    out.sort(key=lambda j: j.created_at, reverse=True)
    return out[:limit]


def cancel_background_task(job_id: str) -> bool:
    """Cancel a running/pending task. Returns True on success,
    False if no such id, not a background task, or already terminal.
    """
    job = job_registry.get(job_id)
    if job is None:
        return False
    if not job.label.startswith(_LABEL_PREFIX):
        return False
    return job_registry.cancel(job_id)


__all__ = [
    "BackgroundTaskSpec",
    "cancel_background_task",
    "list_active_jobs",
    "list_recent_jobs",
    "spawn_background_task",
]
