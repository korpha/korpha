"""In-process job registry."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
"""Completed jobs older than this get evicted on the next prune.
24h is plenty for "what happened with that build I started before
lunch" without unbounded memory growth."""


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    """One background job. Fields are read-write so the runtime can
    update status / result in place; callers should treat as
    read-only after they receive a snapshot from ``snapshot()``."""

    id: str
    label: str
    """Short human-readable description ("ship_via_codex: refactor
    login handler"). Surfaced in CLI / dashboard listings."""

    business_id: str | None = None
    """For multi-tenant filtering. None for system jobs."""

    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    result: Any = None
    """Set when status becomes COMPLETED. Shape is up to the caller —
    typically a dict {'output': str, 'cost_usd': float, ...}."""

    error: str | None = None
    """Set when status becomes FAILED. The exception message —
    not a traceback (those go to the logger)."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Caller-supplied metadata visible in listings (e.g. cwd,
    sandbox_mode, prompt preview)."""

    _task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def snapshot(self) -> "Job":
        """Frozen-ish copy for safe handoff to async callers (so the
        registry can mutate the original without surprising them)."""
        return Job(
            id=self.id,
            label=self.label,
            business_id=self.business_id,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            result=self.result,
            error=self.error,
            extra=dict(self.extra),
        )

    def is_terminal(self) -> bool:
        return self.status in (
            JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED,
        )

    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at


class JobRegistry:
    """Process-wide registry. The module-level ``job_registry``
    instance is what almost everyone wants; tests construct their
    own to stay isolated."""

    def __init__(
        self, *, retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._retention_seconds = retention_seconds

    # ---- start ----

    def submit(
        self,
        coro: Awaitable[Any],
        *,
        label: str,
        business_id: str | None = None,
        extra: dict[str, Any] | None = None,
        on_complete: Callable[["Job"], Awaitable[None]] | None = None,
    ) -> Job:
        """Wrap ``coro`` in an asyncio.Task that updates the job's
        status when it completes. Returns the Job (status RUNNING
        once the task gets scheduler time, PENDING in the brief
        gap between submit() and the first event-loop tick).

        ``coro`` must be an awaitable that returns the result the
        founder cares about (string, dict, anything serializable
        for the eventual ``code.codex_job_result`` skill).

        ``on_complete``, if supplied, is awaited after the job
        reaches a terminal state. Errors raised by ``on_complete``
        are logged and swallowed so a flaky notification path
        never wedges the registry. Use cases: push a Telegram
        message ("build done"), record an Activity row, queue a
        digest entry.
        """
        # Prune opportunistically so the registry doesn't grow
        # unbounded across a long-running server.
        self._prune_expired()

        job_id = uuid.uuid4().hex[:12]
        job = Job(
            id=job_id,
            label=label,
            business_id=business_id,
            extra=dict(extra or {}),
        )
        self._jobs[job_id] = job

        async def _runner() -> None:
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            try:
                result = await coro
                job.result = result
                job.status = JobStatus.COMPLETED
            except asyncio.CancelledError:
                job.status = JobStatus.CANCELLED
                job.error = "cancelled"
                # Re-raise so the asyncio runtime can clean up properly
                raise
            except Exception as exc:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "job %s (%s) failed: %s", job_id, label, exc,
                )
            finally:
                job.finished_at = time.time()
                if on_complete is not None:
                    try:
                        await on_complete(job)
                    except Exception as cb_exc:  # noqa: BLE001
                        logger.warning(
                            "job %s on_complete raised: %s",
                            job_id, cb_exc,
                        )

        task = asyncio.create_task(_runner(), name=f"job-{job_id}")
        job._task = task
        return job

    # ---- read ----

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(
        self,
        *,
        business_id: str | None = None,
        status: JobStatus | None = None,
    ) -> list[Job]:
        """Snapshot of the registry, sorted newest-first. Filters
        compose: pass both to narrow more."""
        out = list(self._jobs.values())
        if business_id is not None:
            out = [j for j in out if j.business_id == business_id]
        if status is not None:
            out = [j for j in out if j.status == status]
        out.sort(key=lambda j: j.created_at, reverse=True)
        return out

    # ---- mutate ----

    def cancel(self, job_id: str) -> bool:
        """Cancel a running job. Returns True if a task was found
        and cancelled, False if the job is unknown or already
        terminal."""
        job = self._jobs.get(job_id)
        if job is None or job.is_terminal():
            return False
        if job._task is None or job._task.done():
            return False
        job._task.cancel()
        return True

    def _prune_expired(self) -> int:
        """Drop terminal jobs older than the retention window.
        Returns the count removed."""
        if not self._jobs:
            return 0
        cutoff = time.time() - self._retention_seconds
        to_remove = [
            jid
            for jid, job in self._jobs.items()
            if job.is_terminal()
            and job.finished_at is not None
            and job.finished_at < cutoff
        ]
        for jid in to_remove:
            del self._jobs[jid]
        return len(to_remove)

    def clear(self) -> None:
        """Drop everything. Tests use this between runs; production
        callers shouldn't need it."""
        for job in list(self._jobs.values()):
            if job._task and not job._task.done():
                job._task.cancel()
        self._jobs.clear()


# Process-wide singleton
job_registry = JobRegistry()
