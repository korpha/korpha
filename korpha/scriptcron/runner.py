"""Execute due ScriptCron jobs + push stdout to the configured
channel. No LLM in the loop."""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.scriptcron.model import ScriptCron, ScriptCronStatus

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS = 60.0
"""Cap script wall-clock. Watchdogs should be fast (memory check, ping
test, RSS pull). A multi-minute build belongs in the agent loop, not
here."""

_MAX_OUTPUT_BYTES = 4096
"""Cap stdout we capture + persist. Most watchdog scripts emit a
single line; deeper output gets truncated to keep the DB row + the
delivered message tractable."""


@dataclass(frozen=True)
class RunOutcome:
    """One tick's outcome. Returned by ``run_job`` for callers that
    want to react (CLI, tests)."""

    job_id: UUID
    status: ScriptCronStatus
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    delivered: bool = False
    error: str = ""


_CADENCE_RE = re.compile(
    r"^\s*every\s+(\d+)\s*(m|min|minutes?|h|hr|hours?|d|days?)\s*$",
    re.IGNORECASE,
)


def parse_cadence(text: str) -> timedelta:
    """``'every 5m'`` → ``timedelta(minutes=5)``. Raises ValueError
    on unparseable input."""
    match = _CADENCE_RE.match(text or "")
    if not match:
        raise ValueError(
            f"cadence {text!r} not understood. Use 'every Nm', "
            "'every Nh', or 'every Nd' (e.g. 'every 12h')."
        )
    n = int(match.group(1))
    if n <= 0:
        raise ValueError(f"cadence interval must be positive, got {n}")
    unit = match.group(2).lower()
    if unit.startswith("m"):
        return timedelta(minutes=n)
    if unit.startswith("h"):
        return timedelta(hours=n)
    return timedelta(days=n)


def _is_due(job: ScriptCron, now: datetime) -> bool:
    if not job.enabled:
        return False
    if job.last_run_at is None:
        return True
    try:
        delta = parse_cadence(job.cadence)
    except ValueError:
        # Malformed cadence — treat as not-due so the job doesn't
        # storm-loop; the founder needs to fix it.
        logger.warning(
            "scriptcron: job %s has bad cadence %r; skipping",
            job.id, job.cadence,
        )
        return False
    # SQLite drops tz on round-trip; Postgres preserves it. Coerce
    # both sides to aware-UTC so the subtraction is portable.
    last = job.last_run_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - last >= delta


def _truncate(text: str, cap: int = _MAX_OUTPUT_BYTES) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 32] + f"\n…[truncated {len(text) - cap + 32}b]"


def _interpreter_for(script: Path) -> list[str]:
    """Pick the interpreter from the file extension. Explicit table
    rather than reading the shebang — predictable + auditable."""
    suffix = script.suffix.lower()
    if suffix in (".sh", ".bash"):
        return ["/bin/bash", str(script)]
    if suffix in (".py",):
        return [sys.executable, str(script)]
    # Default: exec directly. Founder's responsibility to make it
    # executable + carry an OS-level shebang.
    return [str(script)]


async def _run_script(
    script_path: Path, *, timeout_seconds: float,
) -> tuple[int | None, str, str, str]:
    """Run the script, capture stdout/stderr, enforce timeout.
    Returns ``(exit_code, stdout, stderr, error_msg)``. ``error_msg``
    is the runner-level reason on hard failure (timeout, file not
    found, etc.); the script's own stderr is returned separately
    so the operator can see both."""
    if not script_path.exists():
        return (
            None, "", "",
            f"script not found at {script_path}",
        )
    if not script_path.is_file():
        return (
            None, "", "",
            f"script path {script_path} is not a regular file",
        )
    cmd = _interpreter_for(script_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return (
            None, "", "",
            f"interpreter / script not found: {exc}",
        )
    except OSError as exc:
        return (None, "", "", f"could not exec: {exc}")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except (OSError, ProcessLookupError):
            pass
        return (None, "", "", f"timeout after {timeout_seconds}s")

    stdout = _truncate(stdout_b.decode("utf-8", errors="replace").strip())
    stderr = _truncate(stderr_b.decode("utf-8", errors="replace").strip())
    return (proc.returncode, stdout, stderr, "")


async def _deliver(job: ScriptCron, body: str) -> bool:
    """Push ``body`` to the job's configured channel. Returns True
    on successful send. Logs + returns False on transport failure
    so a flaky channel doesn't crash the tick."""
    if not job.deliver_platform or not job.deliver_recipient:
        return False
    from korpha.skills.channel import _PLATFORM_SENDERS

    entry = _PLATFORM_SENDERS.get(job.deliver_platform.lower())
    if entry is None:
        logger.warning(
            "scriptcron: job %s has unknown platform %r; skipping delivery",
            job.id, job.deliver_platform,
        )
        return False
    _, sender = entry
    try:
        await sender(
            recipient=job.deliver_recipient,
            content=body,
            subject=f"[cron] {job.name}",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scriptcron: delivery for job %s on %s failed: %s",
            job.id, job.deliver_platform, exc,
        )
        return False


async def run_job(
    session: Session,
    job: ScriptCron,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> RunOutcome:
    """Execute one job synchronously (relative to the caller).
    Updates the row's last_run_at + last_status + last_output. Pushes
    delivery if configured + warranted. Returns the outcome."""
    now = now if now is not None else datetime.now(tz=timezone.utc)
    exit_code, stdout, stderr, error = await _run_script(
        Path(job.script_path).expanduser(),
        timeout_seconds=timeout_seconds,
    )
    job.last_run_at = now
    delivered = False
    if error:
        # Hard failure: timeout, missing script, exec error
        body = (
            f"❌ cron {job.name} failed: {error}"
            + (f"\n\nstderr:\n{stderr}" if stderr else "")
        )
        delivered = await _deliver(job, body)
        job.last_status = ScriptCronStatus.FAILED
        job.last_output = stdout
        job.last_error = error
    elif exit_code != 0:
        body = (
            f"❌ cron {job.name} exited {exit_code}"
            + (f"\n\nstdout:\n{stdout}" if stdout else "")
            + (f"\n\nstderr:\n{stderr}" if stderr else "")
        )
        delivered = await _deliver(job, body)
        job.last_status = ScriptCronStatus.FAILED
        job.last_output = stdout
        job.last_error = stderr or f"exit {exit_code}"
    elif not stdout:
        # Watchdog pattern: success with no output = silent tick.
        # Don't ping the founder.
        job.last_status = ScriptCronStatus.SILENT
        job.last_output = ""
        job.last_error = ""
    else:
        # Success with output → deliver verbatim.
        delivered = await _deliver(job, stdout)
        job.last_status = ScriptCronStatus.OK
        job.last_output = stdout
        job.last_error = ""
    job.last_summary = _build_run_summary(
        status=job.last_status,
        stdout=stdout, stderr=stderr,
        error=error, exit_code=exit_code,
        delivered=delivered,
    )
    job.updated_at = now
    session.add(job)
    session.commit()
    session.refresh(job)
    return RunOutcome(
        job_id=job.id,
        status=job.last_status,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        delivered=delivered,
        error=error,
    )


def _build_run_summary(
    *,
    status: ScriptCronStatus,
    stdout: str,
    stderr: str,
    error: str,
    exit_code: int,
    delivered: bool,
) -> str:
    """Compose a bounded one-line digest. ≤ 400 chars total —
    enough to render inline on /app/cron without expansion.

    Output shape:
      <status emoji> <first non-empty line of stdout/stderr>  [details]
    """
    # Find the headline line — first non-blank from stdout, or
    # stderr/error on failure paths.
    source = ""
    if status == ScriptCronStatus.FAILED:
        source = (stderr or error or stdout or "").strip()
    elif status == ScriptCronStatus.OK:
        source = stdout.strip()
    elif status == ScriptCronStatus.SILENT:
        return "✓ silent — clean tick"

    headline = ""
    for line in source.splitlines():
        cleaned = line.strip()
        if cleaned:
            headline = cleaned[:200]
            break

    emoji = "✓" if status == ScriptCronStatus.OK else "✗"
    suffix_parts = []
    if exit_code:
        suffix_parts.append(f"exit {exit_code}")
    if status == ScriptCronStatus.OK and delivered:
        suffix_parts.append("delivered")
    suffix = (
        " [" + " · ".join(suffix_parts) + "]"
        if suffix_parts else ""
    )
    body = f"{emoji} {headline}{suffix}"
    return body[:400]


async def run_due_jobs(
    session: Session,
    *,
    business_id: UUID | None = None,
    now: datetime | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[RunOutcome]:
    """Find every enabled job that's due + run them. Multi-tenant:
    pass ``business_id`` to scope; omit to scan everyone (ops
    job, not founder-facing). Returns one outcome per job that ran."""
    now = now if now is not None else datetime.now(tz=timezone.utc)
    stmt = select(ScriptCron).where(ScriptCron.enabled == True)  # noqa: E712
    if business_id is not None:
        stmt = stmt.where(ScriptCron.business_id == business_id)
    candidates = list(session.exec(stmt).all())
    outcomes: list[RunOutcome] = []
    for job in candidates:
        if not _is_due(job, now):
            continue
        try:
            out = await run_job(
                session, job, timeout_seconds=timeout_seconds, now=now,
            )
        except Exception as exc:  # noqa: BLE001
            # Don't let one broken job poison the loop
            logger.warning(
                "scriptcron: run_job for %s raised: %s", job.id, exc,
            )
            continue
        outcomes.append(out)
    return outcomes


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RunOutcome",
    "parse_cadence",
    "run_due_jobs",
    "run_job",
]
