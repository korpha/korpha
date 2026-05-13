"""code.ship_via_codex — CTO dispatches real code work to Codex CLI.

Closes the loop on "the AI cofounder can actually change code in the
repo." The CTO produces a plan; this skill takes the plan + a working
directory and runs ``codex exec`` non-interactively with the user's
ChatGPT subscription doing the heavy lifting.

Why approval-gated: code changes are irreversible side effects.
``ActionClass.SHIP`` makes this fall under the standard ApprovalGate
trust envelope. The Founder sees what's about to be dispatched, can
revise the prompt, and only after approval does the actual ``codex``
subprocess fire.

Why ``read-only`` is NOT the default here (it is for the inference
provider): this skill exists specifically to write code. Callers can
override ``sandbox_mode`` to ``workspace-write`` (default) or
``danger-full-access`` (rare; e.g. infra-edit work that needs network).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from korpha.audit.model import InferenceTier
from korpha.delegation import CodexCLI, DelegationError, DelegationRequest
from korpha.inference.limits import coding_max_tokens
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)


class ShipViaCodexSkill(Skill):
    """Dispatch a coding task to Codex CLI in the user's repo.

    Use case: CTO scoped a work item ("add a /healthz endpoint to the
    FastAPI app, return JSON {status: ok}, write a test"). Instead of
    surfacing it as a blocker for Mike to copy-paste into Codex
    himself, the CEO/CTO route it through this skill — Codex CLI runs
    in his repo with his subscription, reports back, the diff lands
    awaiting his review.
    """

    spec = SkillSpec(
        name="code.ship_via_codex",
        description=(
            "Dispatch a coding task to Codex CLI (uses your ChatGPT "
            "subscription, $0 marginal). Codex runs in your repo, "
            "writes / edits files, runs tests if asked, reports back "
            "the summary + any errors. The actual diff sits in your "
            "working tree for you to review — nothing is committed."
        ),
        parameters={
            "prompt": (
                "What to do, in plain English. Be specific — Codex is "
                "a real coding agent and benefits from concrete asks "
                "('Add /healthz to api/main.py returning {status: ok}; "
                "write a test in tests/test_health.py')."
            ),
            "cwd": (
                "Working directory (repo root). Defaults to the "
                "Founder's current project workspace."
            ),
            "sandbox_mode": (
                "Codex sandbox policy. 'workspace-write' (default) "
                "lets it edit files in cwd. 'read-only' for review-"
                "only runs. 'danger-full-access' for infra/network — "
                "use sparingly."
            ),
            "wait": (
                "If true (default), block until Codex finishes and "
                "return the result. If false, fire as a background "
                "job and return a job_id immediately — use when the "
                "task is long (>2 min) so the chat doesn't freeze. "
                "Founder can ask 'is build X done?' via "
                "code.codex_job_status, or 'what happened with X?' "
                "via code.codex_job_result."
            ),
            "notify_on_complete": (
                "Comma-separated channels to ping when the "
                "background job finishes. Currently supports "
                "'email' and 'telegram'. Recipient is read from "
                "KORPHA_NOTIFY_EMAIL / KORPHA_NOTIFY_TELEGRAM. "
                "Only used when wait=False. Empty (default) = no "
                "push (founder polls via code.codex_job_status)."
            ),
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise SkillError(
                "code.ship_via_codex requires `prompt` — describe what "
                "Codex should do."
            )

        cwd = args.get("cwd")
        if cwd is None:
            # Default to the workspace dir if the runtime exposes one;
            # otherwise the Founder's cwd. Workspace path lives on the
            # business runtime context when set up.
            ws = getattr(ctx.business, "workspace_path", None)
            cwd = str(ws) if ws else None

        if cwd is not None and not Path(cwd).expanduser().exists():
            raise SkillError(
                f"code.ship_via_codex: cwd {cwd!r} does not exist. "
                "Make sure the Founder's project workspace is set up."
            )

        sandbox_mode = str(args.get("sandbox_mode") or "workspace-write")
        if sandbox_mode not in ("read-only", "workspace-write", "danger-full-access"):
            raise SkillError(
                f"code.ship_via_codex: invalid sandbox_mode {sandbox_mode!r}. "
                "Use 'read-only' | 'workspace-write' | 'danger-full-access'."
            )

        if shutil.which("codex") is None:
            raise SkillError(
                "Codex CLI not found on PATH. Install with `npm install "
                "-g @openai/codex` and run `codex login` (uses your "
                "ChatGPT subscription)."
            )

        # Enrich the prompt with any AGENTS.md / CLAUDE.md / .cursorrules
        # discovered in the cwd's subtree. Codex itself reads these as
        # it navigates, but quoting the relevant rules in the OPENING
        # prompt sets framing earlier and surfaces conflicts before
        # the first edit. Cheap (one or two file reads); huge quality
        # lift on real user repos that codify conventions.
        if cwd is not None:
            from korpha.skills.subdir_hints import SubdirectoryHintTracker
            try:
                tracker = SubdirectoryHintTracker(
                    working_dir=Path(cwd).expanduser(),
                    # One-shot Codex prompt build — emit root hints
                    # too (Codex hasn't seen them yet).
                    assume_root_visited=False,
                )
                # The agent didn't necessarily navigate yet; "visit"
                # the cwd itself (and let any referenced paths in the
                # prompt also pull hints).
                hints = tracker.hints_for_paths(
                    [Path(cwd).expanduser() / "."]
                ) or tracker.hints_for_command(prompt)
            except Exception as exc:  # noqa: BLE001
                # Discovery is best-effort; never block a Codex call
                # over a stat failure or unicode quirk in a stray file.
                logger.debug("subdir_hints discovery failed: %s", exc)
                hints = None
            if hints:
                prompt = (
                    f"{hints}\n\n"
                    f"---\n\nFollow the project conventions above "
                    f"when relevant.\n\n"
                    f"{prompt}"
                )

        # Pre-snapshot the workspace so a destructive Codex run
        # is recoverable. Best-effort — if snapshotting fails (huge
        # workspace, permissions, etc.), log and proceed rather
        # than blocking the founder's task. ``korpha checkpoints
        # restore`` is the undo path.
        pre_snapshot_id: str | None = None
        if (
            cwd is not None
            and sandbox_mode != "read-only"
            and Path(cwd).expanduser().is_dir()
        ):
            from korpha.checkpoints import (
                CheckpointError, snapshot,
            )
            try:
                cp = snapshot(
                    Path(cwd).expanduser(),
                    label=f"pre-codex: {prompt[:60]}",
                )
                pre_snapshot_id = cp.id
                logger.info(
                    "code.ship_via_codex: snapshot %s before Codex run",
                    cp.id,
                )
            except CheckpointError as exc:
                logger.warning(
                    "code.ship_via_codex: pre-snapshot failed (%s); "
                    "Codex will run without rollback safety net", exc,
                )

        cli = CodexCLI(sandbox_mode=sandbox_mode)
        request = DelegationRequest(
            prompt=prompt,
            cwd=str(Path(cwd).expanduser()) if cwd else None,
            timeout_seconds=float(coding_max_tokens()) / 100.0
            if coding_max_tokens() > 30_000
            else 600.0,
            # ^ rough proxy: bigger token budget → longer expected wall
            # clock. 600s baseline matches typical multi-step coding
            # loops; users can override per-call.
        )

        # Background mode: return immediately with a job_id. The
        # founder asks code.codex_job_status / code.codex_job_result
        # to follow up. Useful for multi-minute builds where blocking
        # the chat thread would frustrate Mike.
        wait = bool(args.get("wait", True))
        if not wait:
            from korpha.jobs import job_registry

            async def _bg_codex_run() -> dict[str, Any]:
                resp = await cli.run(request)
                return {
                    "content": resp.content,
                    "raw_output": resp.raw_output,
                    "cost_usd": float(getattr(resp, "cost_usd", 0.0) or 0.0),
                    "cwd": cwd,
                    "sandbox_mode": sandbox_mode,
                    "pre_snapshot_id": pre_snapshot_id,
                }

            business_id = getattr(ctx.business, "id", None)
            notify_raw = str(args.get("notify_on_complete") or "").strip()
            channels = [
                ch.strip().lower()
                for ch in notify_raw.split(",")
                if ch.strip()
            ] if notify_raw else []
            on_complete = (
                _build_codex_notifier(channels)
                if channels else None
            )
            job = job_registry.submit(
                _bg_codex_run(),
                label=f"ship_via_codex: {prompt[:60]}",
                business_id=str(business_id) if business_id else None,
                extra={
                    "cwd": cwd,
                    "sandbox_mode": sandbox_mode,
                    "pre_snapshot_id": pre_snapshot_id,
                    "notify_channels": channels,
                },
                on_complete=on_complete,
            )
            return SkillResult(
                skill_name=self.spec.name,
                summary=(
                    f"Codex started in background as job "
                    f"{job.id} (cwd={cwd or 'caller cwd'}, "
                    f"sandbox={sandbox_mode}). Check progress with "
                    f"code.codex_job_status, get the full output with "
                    f"code.codex_job_result when finished."
                ),
                payload={
                    "job_id": job.id,
                    "status": job.status.value,
                    "background": True,
                    "cwd": cwd,
                    "sandbox_mode": sandbox_mode,
                    "pre_snapshot_id": pre_snapshot_id,
                },
                cost_usd=0.0,
            )

        try:
            response = await cli.run(request)
        except DelegationError as exc:
            raise SkillError(f"code.ship_via_codex: {exc}") from exc

        summary = (
            f"Codex CLI dispatched in {cwd or 'caller cwd'} "
            f"(sandbox={sandbox_mode}). Summary:\n\n"
            f"{response.content[:1200]}"
            + ("\n\n…(output truncated)" if len(response.content) > 1200 else "")
        )
        if pre_snapshot_id:
            summary += (
                f"\n\nWorkspace snapshot {pre_snapshot_id} taken before "
                "this run — to undo, run "
                f"`korpha checkpoints restore {pre_snapshot_id}`."
            )

        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={
                "cwd": cwd,
                "sandbox_mode": sandbox_mode,
                "codex_output": response.content,
                "raw_output": response.raw_output,
                "pre_snapshot_id": pre_snapshot_id,
            },
            cost_usd=0.0,  # Codex CLI is subscription-paid, no per-call meter
        )


class CodexJobStatusSkill(Skill):
    """Look up an in-flight or recently-completed Codex background
    job. The CEO router calls this when the founder asks "is build X
    done?" or just "what's running?"."""

    spec = SkillSpec(
        name="code.codex_job_status",
        description=(
            "Check the status of a background Codex job. Returns "
            "running / completed / failed / cancelled + how long "
            "it's been going. Use when the founder asks if a "
            "previously-started codex run is finished. Pass no "
            "job_id to list every running + recently-completed job."
        ),
        parameters={
            "job_id": (
                "The job id from a previous code.ship_via_codex "
                "background call. Optional — omit to list every "
                "job for this business."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        from korpha.jobs import job_registry

        job_id = str(args.get("job_id") or "").strip()
        business_id = (
            str(getattr(ctx.business, "id", "") or "") or None
        )

        if job_id:
            job = job_registry.get(job_id)
            if job is None:
                raise SkillError(
                    f"code.codex_job_status: job {job_id!r} not found "
                    "(may have been pruned or never existed)."
                )
            # Multi-tenant safety — don't leak another business's job
            if (
                business_id is not None
                and job.business_id is not None
                and job.business_id != business_id
            ):
                raise SkillError(
                    f"code.codex_job_status: job {job_id!r} not "
                    "owned by this business."
                )
            duration = job.duration_seconds()
            return SkillResult(
                skill_name=self.spec.name,
                summary=(
                    f"Job {job.id} ({job.label}): {job.status.value}"
                    + (f" ({duration:.1f}s)" if duration is not None else "")
                ),
                payload={
                    "job_id": job.id,
                    "status": job.status.value,
                    "label": job.label,
                    "duration_seconds": duration,
                    "error": job.error,
                    "extra": dict(job.extra),
                },
                cost_usd=0.0,
            )

        jobs = job_registry.list(business_id=business_id)
        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"{len(jobs)} job(s) tracked for this business."
                if jobs else
                "No background jobs running for this business."
            ),
            payload={
                "jobs": [
                    {
                        "job_id": j.id,
                        "label": j.label,
                        "status": j.status.value,
                        "duration_seconds": j.duration_seconds(),
                    }
                    for j in jobs
                ],
            },
            cost_usd=0.0,
        )


class CodexJobResultSkill(Skill):
    """Fetch the full output of a completed Codex background job.
    The agent surfaces the codex_output to the founder so they can
    review what changed."""

    spec = SkillSpec(
        name="code.codex_job_result",
        description=(
            "Fetch the full result of a completed Codex background "
            "job. Returns the Codex output + cwd + the pre-snapshot "
            "id (so you can mention 'to undo, restore X'). Errors "
            "if the job is still running or doesn't exist."
        ),
        parameters={
            "job_id": (
                "The job id from a previous code.ship_via_codex "
                "background call."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        from korpha.jobs import JobStatus, job_registry

        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            raise SkillError(
                "code.codex_job_result: job_id is required."
            )
        job = job_registry.get(job_id)
        if job is None:
            raise SkillError(
                f"code.codex_job_result: job {job_id!r} not found."
            )
        business_id = (
            str(getattr(ctx.business, "id", "") or "") or None
        )
        if (
            business_id is not None
            and job.business_id is not None
            and job.business_id != business_id
        ):
            raise SkillError(
                f"code.codex_job_result: job {job_id!r} not owned "
                "by this business."
            )
        if job.status == JobStatus.RUNNING:
            raise SkillError(
                f"code.codex_job_result: job {job_id!r} is still "
                "running. Try again in a minute."
            )
        if job.status == JobStatus.PENDING:
            raise SkillError(
                f"code.codex_job_result: job {job_id!r} hasn't "
                "started yet — try again."
            )
        if job.status == JobStatus.FAILED:
            return SkillResult(
                skill_name=self.spec.name,
                summary=(
                    f"Job {job.id} failed: {job.error or '(no detail)'}"
                ),
                payload={
                    "job_id": job.id,
                    "status": job.status.value,
                    "error": job.error,
                    "extra": dict(job.extra),
                },
                cost_usd=0.0,
            )
        if job.status == JobStatus.CANCELLED:
            return SkillResult(
                skill_name=self.spec.name,
                summary=f"Job {job.id} was cancelled.",
                payload={
                    "job_id": job.id, "status": job.status.value,
                },
                cost_usd=0.0,
            )
        # Completed
        result = job.result if isinstance(job.result, dict) else {}
        content = str(result.get("content") or "")
        snap_id = result.get("pre_snapshot_id")
        summary = (
            f"Job {job.id} completed. Codex output:\n\n"
            f"{content[:1500]}"
            + ("\n\n…(output truncated)" if len(content) > 1500 else "")
        )
        if snap_id:
            summary += (
                f"\n\nWorkspace snapshot {snap_id} was taken "
                "before this run — to undo, run "
                f"`korpha checkpoints restore {snap_id}`."
            )
        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={
                "job_id": job.id,
                "status": job.status.value,
                "duration_seconds": job.duration_seconds(),
                **result,
            },
            cost_usd=float(result.get("cost_usd") or 0.0),
        )


def _build_codex_notifier(channels: list[str]):
    """Build the on_complete coroutine that pushes a job-finished
    notification to each configured channel. Reads recipients from
    env so we don't have to plumb through the founder's address.

    Returns ``None`` when no usable channels are listed (caller
    skips the on_complete altogether)."""
    import os

    from korpha.jobs import Job, JobStatus

    valid = [
        ch for ch in channels if ch in ("email", "telegram")
    ]
    if not valid:
        return None

    async def _notify(job: Job) -> None:
        # Lazy import — keep cold-load cost off founders not using
        # background jobs at all.
        from korpha.skills.channel import _PLATFORM_SENDERS

        # Build a one-line headline + small detail block. The exact
        # format is what shows up on the founder's phone.
        if job.status == JobStatus.COMPLETED:
            headline = f"✓ Codex job {job.id} finished"
            result = job.result if isinstance(job.result, dict) else {}
            content = (
                str(result.get("content") or "")[:600]
            )
            snap = result.get("pre_snapshot_id")
            dur = job.duration_seconds()
            dur_str = f"{dur:.0f}s" if dur is not None else "—"
            body = headline + "\n\n" + (
                f"Job: {job.label}\n"
                f"Duration: {dur_str}\n\n"
                f"Output:\n{content}"
                + (
                    f"\n\nUndo: korpha checkpoints restore {snap}"
                    if snap else ""
                )
            )
        elif job.status == JobStatus.FAILED:
            body = (
                f"✗ Codex job {job.id} failed\n\n"
                f"Job: {job.label}\n"
                f"Error: {job.error or '(no detail)'}"
            )
        elif job.status == JobStatus.CANCELLED:
            body = f"✗ Codex job {job.id} was cancelled."
        else:
            return

        for channel in valid:
            recipient = os.environ.get(
                f"KORPHA_NOTIFY_{channel.upper()}"
            )
            if not recipient:
                # Operator wanted notifications but didn't set the
                # env var. Log and skip — better than crashing the
                # job's finalization.
                logger.warning(
                    "ship_via_codex: notify=%s requested but "
                    "KORPHA_NOTIFY_%s not set; skipping",
                    channel, channel.upper(),
                )
                continue
            _, sender = _PLATFORM_SENDERS[channel]
            try:
                await sender(
                    recipient=recipient,
                    content=body,
                    subject=f"Codex job {job.id}: {job.status.value}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ship_via_codex: %s notification failed: %s",
                    channel, exc,
                )

    return _notify


register(ShipViaCodexSkill())
register(CodexJobStatusSkill())
register(CodexJobResultSkill())


__all__ = [
    "CodexJobResultSkill",
    "CodexJobStatusSkill",
    "ShipViaCodexSkill",
]
