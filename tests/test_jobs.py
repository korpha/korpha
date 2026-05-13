"""Tests for the background-job runtime + Codex async path.

Covers:
  - JobRegistry submit / status transitions / cancel / prune /
    multi-tenant filtering
  - code.ship_via_codex with wait=False returns a job_id immediately
    + the actual Codex runs in the background
  - code.codex_job_status finds + filters + multi-tenant safe
  - code.codex_job_result errors when running, returns content
    when complete, returns error when failed
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from korpha.jobs import (
    DEFAULT_RETENTION_SECONDS,
    Job,
    JobRegistry,
    JobStatus,
)


@pytest.fixture
def registry() -> JobRegistry:
    return JobRegistry()


# ---- JobRegistry: submit + transitions ----


@pytest.mark.asyncio
async def test_submit_returns_job_with_pending_status(
    registry: JobRegistry,
) -> None:
    async def _noop() -> str:
        return "ok"
    job = registry.submit(_noop(), label="test-noop")
    # Brand-new submit hasn't yielded to scheduler — pending OR running
    assert job.status in (JobStatus.PENDING, JobStatus.RUNNING)
    # After a tick, the runner runs and completes
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert job.status == JobStatus.COMPLETED
    assert job.result == "ok"


@pytest.mark.asyncio
async def test_failed_job_records_error_message(
    registry: JobRegistry,
) -> None:
    async def _boom() -> str:
        raise RuntimeError("kapow")
    job = registry.submit(_boom(), label="test-fail")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert job.status == JobStatus.FAILED
    assert "kapow" in job.error  # type: ignore[operator]


@pytest.mark.asyncio
async def test_duration_seconds_set_after_completion(
    registry: JobRegistry,
) -> None:
    async def _slow() -> str:
        await asyncio.sleep(0.05)
        return "done"
    job = registry.submit(_slow(), label="t")
    await job._task  # type: ignore[arg-type]
    assert job.duration_seconds() is not None
    assert job.duration_seconds() >= 0.04


# ---- cancel ----


@pytest.mark.asyncio
async def test_cancel_running_job(registry: JobRegistry) -> None:
    async def _long() -> str:
        await asyncio.sleep(60)
        return "never"
    job = registry.submit(_long(), label="t")
    await asyncio.sleep(0.01)  # let it start
    cancelled = registry.cancel(job.id)
    assert cancelled is True
    # Drain the cancellation
    with pytest.raises(asyncio.CancelledError):
        await job._task  # type: ignore[arg-type]
    assert job.status == JobStatus.CANCELLED


def test_cancel_unknown_job_returns_false(
    registry: JobRegistry,
) -> None:
    assert registry.cancel("does-not-exist") is False


@pytest.mark.asyncio
async def test_cancel_already_completed_returns_false(
    registry: JobRegistry,
) -> None:
    async def _quick() -> str:
        return "ok"
    job = registry.submit(_quick(), label="t")
    await job._task  # type: ignore[arg-type]
    assert registry.cancel(job.id) is False


# ---- list filtering ----


@pytest.mark.asyncio
async def test_list_filters_by_business_id(
    registry: JobRegistry,
) -> None:
    async def _ok() -> str:
        return "ok"
    a = registry.submit(_ok(), label="a", business_id="biz-a")
    b = registry.submit(_ok(), label="b", business_id="biz-b")
    c = registry.submit(_ok(), label="c", business_id="biz-a")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    just_a = registry.list(business_id="biz-a")
    assert {j.id for j in just_a} == {a.id, c.id}


@pytest.mark.asyncio
async def test_list_filters_by_status(registry: JobRegistry) -> None:
    async def _ok() -> str:
        return "ok"
    async def _bad() -> str:
        raise RuntimeError("x")
    ok = registry.submit(_ok(), label="o")
    bad = registry.submit(_bad(), label="b")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    completed = registry.list(status=JobStatus.COMPLETED)
    assert {j.id for j in completed} == {ok.id}


@pytest.mark.asyncio
async def test_list_sorted_newest_first(registry: JobRegistry) -> None:
    async def _ok() -> str:
        return "ok"
    a = registry.submit(_ok(), label="a")
    time.sleep(0.001)  # ensure created_at differs
    b = registry.submit(_ok(), label="b")
    rows = registry.list()
    assert rows[0].id == b.id
    assert rows[1].id == a.id


# ---- prune ----


@pytest.mark.asyncio
async def test_prune_drops_old_terminal_jobs() -> None:
    reg = JobRegistry(retention_seconds=0.01)

    async def _ok() -> str:
        return "x"
    j = reg.submit(_ok(), label="t")
    await j._task  # type: ignore[arg-type]
    # Force finished_at into the past so it falls below the cutoff
    j.finished_at = time.time() - 1.0
    removed = reg._prune_expired()
    assert removed == 1
    assert reg.get(j.id) is None


@pytest.mark.asyncio
async def test_prune_leaves_running_jobs_alone() -> None:
    reg = JobRegistry(retention_seconds=0.001)

    async def _long() -> str:
        await asyncio.sleep(60)
        return "x"
    j = reg.submit(_long(), label="long")
    await asyncio.sleep(0.01)  # let it start running
    removed = reg._prune_expired()
    assert removed == 0
    assert reg.get(j.id) is not None
    reg.cancel(j.id)


# ---- snapshot ----


@pytest.mark.asyncio
async def test_snapshot_is_independent_of_live_job(
    registry: JobRegistry,
) -> None:
    """snapshot() returns a copy whose status doesn't change when
    the live job moves on."""
    async def _delayed() -> str:
        await asyncio.sleep(0.05)
        return "x"
    job = registry.submit(_delayed(), label="t")
    snap = job.snapshot()
    assert snap.status in (JobStatus.PENDING, JobStatus.RUNNING)
    await job._task  # type: ignore[arg-type]
    # Snap unchanged; live has moved on
    assert snap.status in (JobStatus.PENDING, JobStatus.RUNNING)
    assert job.status == JobStatus.COMPLETED


# ---- code.ship_via_codex wait=False ----


@pytest.fixture
def codex_skill_ctx(tmp_path: Path):
    """Build a minimal SkillContext + tmp repo so the skill can run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("# code")
    from korpha.skills.types import SkillContext

    class _Bus:
        id = "bus-1"
        workspace_path = repo
    class _Founder: pass

    ctx = SkillContext(
        business=_Bus(),
        founder=_Founder(),
        session=None, cost_tracker=None, invoking_agent_role_id=None,
    )
    return ctx, repo


@pytest.mark.asyncio
async def test_ship_via_codex_wait_false_returns_job_id_immediately(
    codex_skill_ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skill should return a job_id without waiting for Codex
    to finish. The job runs in the background."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    ctx, repo = codex_skill_ctx
    from korpha.delegation import (
        DelegationRequest, DelegationResponse,
    )
    from korpha.jobs import job_registry
    from korpha.skills import code_deploy

    finished = asyncio.Event()

    class _SlowCli:
        def __init__(self, *_a, **_k) -> None:
            pass
        async def run(self, _r: DelegationRequest) -> DelegationResponse:
            await asyncio.sleep(0.05)
            finished.set()
            return DelegationResponse(
                content="codex ran fine", raw_output="x", cost_usd=0.0,
            )

    monkeypatch.setattr(code_deploy, "CodexCLI", _SlowCli)
    monkeypatch.setattr(
        "korpha.skills.code_deploy.shutil.which",
        lambda _name: "/usr/bin/codex",
    )

    skill = code_deploy.ShipViaCodexSkill()
    t0 = time.time()
    result = await skill.run(
        ctx=ctx,
        args={
            "prompt": "do thing", "cwd": str(repo), "wait": False,
        },
    )
    elapsed = time.time() - t0
    # Should return faster than the 0.05s sleep — non-blocking
    assert elapsed < 0.04
    assert result.payload["background"] is True
    job_id = result.payload["job_id"]
    assert job_id

    # Now drain the actual background work
    await asyncio.wait_for(finished.wait(), timeout=2.0)
    await asyncio.sleep(0)
    job = job_registry.get(job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.result["content"] == "codex ran fine"
    job_registry.clear()


# ---- code.codex_job_status ----


@pytest.mark.asyncio
async def test_codex_job_status_returns_running_state() -> None:
    from korpha.jobs import job_registry
    from korpha.skills.code_deploy import CodexJobStatusSkill
    from korpha.skills.types import SkillContext

    job_registry.clear()

    async def _slow() -> dict:
        await asyncio.sleep(60)
        return {"content": "x"}
    j = job_registry.submit(
        _slow(), label="t", business_id="bus-x",
    )
    await asyncio.sleep(0.01)

    class _Bus:
        id = "bus-x"
    ctx = SkillContext(
        business=_Bus(), founder=MagicMock(), session=None,
        cost_tracker=None, invoking_agent_role_id=None,
    )
    skill = CodexJobStatusSkill()
    result = await skill.run(ctx=ctx, args={"job_id": j.id})
    assert result.payload["status"] == "running"
    job_registry.cancel(j.id)
    job_registry.clear()


@pytest.mark.asyncio
async def test_codex_job_status_lists_all_when_no_id() -> None:
    from korpha.jobs import job_registry
    from korpha.skills.code_deploy import CodexJobStatusSkill
    from korpha.skills.types import SkillContext

    job_registry.clear()

    async def _ok() -> dict:
        return {"content": "ok"}
    j1 = job_registry.submit(
        _ok(), label="a", business_id="bus-x",
    )
    j2 = job_registry.submit(
        _ok(), label="b", business_id="bus-x",
    )
    job_registry.submit(
        _ok(), label="other", business_id="bus-y",  # different biz
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    class _Bus:
        id = "bus-x"
    ctx = SkillContext(
        business=_Bus(), founder=MagicMock(), session=None,
        cost_tracker=None, invoking_agent_role_id=None,
    )
    skill = CodexJobStatusSkill()
    result = await skill.run(ctx=ctx, args={})
    ids = {j["job_id"] for j in result.payload["jobs"]}
    assert ids == {j1.id, j2.id}  # other-business filtered out
    job_registry.clear()


@pytest.mark.asyncio
async def test_codex_job_status_refuses_other_business_job() -> None:
    """Multi-tenant safety: founder of business A can't peek at
    business B's jobs even if they know the id."""
    from korpha.jobs import job_registry
    from korpha.skills.code_deploy import CodexJobStatusSkill
    from korpha.skills.types import SkillContext, SkillError

    job_registry.clear()

    async def _ok() -> dict:
        return {"content": "x"}
    j = job_registry.submit(
        _ok(), label="other-biz", business_id="bus-y",
    )
    await asyncio.sleep(0)

    class _Bus:
        id = "bus-x"
    ctx = SkillContext(
        business=_Bus(), founder=MagicMock(), session=None,
        cost_tracker=None, invoking_agent_role_id=None,
    )
    skill = CodexJobStatusSkill()
    with pytest.raises(SkillError, match="not owned"):
        await skill.run(ctx=ctx, args={"job_id": j.id})
    job_registry.clear()


# ---- code.codex_job_result ----


@pytest.mark.asyncio
async def test_codex_job_result_returns_content_when_completed() -> None:
    from korpha.jobs import job_registry
    from korpha.skills.code_deploy import CodexJobResultSkill
    from korpha.skills.types import SkillContext

    job_registry.clear()

    async def _ok() -> dict:
        return {
            "content": "diff applied successfully",
            "raw_output": "x",
            "cost_usd": 0.0,
            "pre_snapshot_id": "abc123",
        }
    j = job_registry.submit(
        _ok(), label="ship", business_id="bus-x",
    )
    await j._task  # type: ignore[arg-type]

    class _Bus:
        id = "bus-x"
    ctx = SkillContext(
        business=_Bus(), founder=MagicMock(), session=None,
        cost_tracker=None, invoking_agent_role_id=None,
    )
    skill = CodexJobResultSkill()
    result = await skill.run(ctx=ctx, args={"job_id": j.id})
    assert result.payload["status"] == "completed"
    assert "diff applied" in result.payload["content"]
    # Summary should mention the snapshot id for the undo path
    assert "abc123" in result.summary
    job_registry.clear()


@pytest.mark.asyncio
async def test_codex_job_result_errors_when_still_running() -> None:
    from korpha.jobs import job_registry
    from korpha.skills.code_deploy import CodexJobResultSkill
    from korpha.skills.types import SkillContext, SkillError

    job_registry.clear()

    async def _slow() -> dict:
        await asyncio.sleep(60)
        return {"content": "x"}
    j = job_registry.submit(_slow(), label="t", business_id="b")
    await asyncio.sleep(0.01)

    class _Bus:
        id = "b"
    ctx = SkillContext(
        business=_Bus(), founder=MagicMock(), session=None,
        cost_tracker=None, invoking_agent_role_id=None,
    )
    skill = CodexJobResultSkill()
    with pytest.raises(SkillError, match="still running"):
        await skill.run(ctx=ctx, args={"job_id": j.id})
    job_registry.cancel(j.id)
    job_registry.clear()


# ---- on_complete hook ----


@pytest.mark.asyncio
async def test_on_complete_fires_for_successful_job(
    registry: JobRegistry,
) -> None:
    seen: list[str] = []

    async def _cb(job: Job) -> None:
        seen.append(job.status.value)

    async def _ok() -> str:
        return "x"
    j = registry.submit(_ok(), label="t", on_complete=_cb)
    await j._task  # type: ignore[arg-type]
    # Yield once more so the finally-block's await on_complete runs
    await asyncio.sleep(0)
    assert seen == ["completed"]


@pytest.mark.asyncio
async def test_on_complete_fires_for_failed_job(
    registry: JobRegistry,
) -> None:
    seen: list[str] = []

    async def _cb(job: Job) -> None:
        seen.append(job.status.value)

    async def _bad() -> str:
        raise RuntimeError("x")
    j = registry.submit(_bad(), label="t", on_complete=_cb)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == ["failed"]


@pytest.mark.asyncio
async def test_on_complete_exception_swallowed(
    registry: JobRegistry,
) -> None:
    """A flaky notifier callback shouldn't wedge the registry —
    the exception is logged + the job still terminates cleanly."""
    async def _cb(job: Job) -> None:
        raise RuntimeError("notifier down")

    async def _ok() -> str:
        return "ok"
    j = registry.submit(_ok(), label="t", on_complete=_cb)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Job is still completed despite the callback exploding
    assert j.status == JobStatus.COMPLETED


# ---- codex notifier wiring ----


@pytest.mark.asyncio
async def test_codex_notifier_pushes_on_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ship_via_codex(wait=False, notify_on_complete='email')
    actually invokes the email sender after Codex finishes."""
    from korpha.skills.code_deploy import _build_codex_notifier

    sent: list[dict] = []

    async def _stub_email_sender(
        *, recipient: str, content: str, subject: str | None,
    ) -> dict:
        sent.append({
            "recipient": recipient,
            "subject": subject,
            "content": content,
        })
        return {"to": recipient}

    # Stub the sender table so we don't try to actually call Resend
    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("RESEND_API_KEY",), _stub_email_sender)},
    )
    monkeypatch.setenv("KORPHA_NOTIFY_EMAIL", "mike@example.com")

    cb = _build_codex_notifier(["email"])
    assert cb is not None

    # Build a fake completed job
    j = Job(id="abc123", label="ship_via_codex: refactor")
    j.status = JobStatus.COMPLETED
    j.started_at = time.time() - 12.0
    j.finished_at = time.time()
    j.result = {
        "content": "applied 3 patches",
        "pre_snapshot_id": "snap999",
    }
    await cb(j)
    assert len(sent) == 1
    assert sent[0]["recipient"] == "mike@example.com"
    assert "Codex job abc123 finished" in sent[0]["content"]
    assert "snap999" in sent[0]["content"]


@pytest.mark.asyncio
async def test_codex_notifier_skips_unknown_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_on_complete='slack' (not yet supported) should result
    in no notifier — _build returns None."""
    from korpha.skills.code_deploy import _build_codex_notifier
    cb = _build_codex_notifier(["slack"])
    assert cb is None


@pytest.mark.asyncio
async def test_codex_notifier_no_recipient_logs_and_skips(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """If channel listed but env recipient missing, log warning +
    skip rather than crash the job's finalization."""
    from korpha.skills.code_deploy import _build_codex_notifier

    async def _never_called(**_kw) -> dict:
        raise AssertionError("sender should not be invoked")

    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("RESEND_API_KEY",), _never_called)},
    )
    monkeypatch.delenv("KORPHA_NOTIFY_EMAIL", raising=False)

    cb = _build_codex_notifier(["email"])
    j = Job(id="x", label="t")
    j.status = JobStatus.COMPLETED
    j.finished_at = time.time()
    j.result = {"content": "ok"}
    # Should NOT raise
    await cb(j)


@pytest.mark.asyncio
async def test_codex_notifier_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korpha.skills.code_deploy import _build_codex_notifier

    sent: list[str] = []

    async def _stub(
        *, recipient: str, content: str, subject: str | None,
    ) -> dict:
        sent.append(content)
        return {"to": recipient}

    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("X",), _stub)},
    )
    monkeypatch.setenv("KORPHA_NOTIFY_EMAIL", "x@y.com")

    cb = _build_codex_notifier(["email"])
    j = Job(id="zz", label="codex: bad-thing")
    j.status = JobStatus.FAILED
    j.error = "auth expired"
    j.finished_at = time.time()
    await cb(j)
    assert len(sent) == 1
    assert "✗" in sent[0]
    assert "auth expired" in sent[0]


@pytest.mark.asyncio
async def test_codex_job_result_returns_error_when_failed() -> None:
    from korpha.jobs import job_registry
    from korpha.skills.code_deploy import CodexJobResultSkill
    from korpha.skills.types import SkillContext

    job_registry.clear()

    async def _bad() -> dict:
        raise RuntimeError("codex auth expired")
    j = job_registry.submit(_bad(), label="t", business_id="b")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    class _Bus:
        id = "b"
    ctx = SkillContext(
        business=_Bus(), founder=MagicMock(), session=None,
        cost_tracker=None, invoking_agent_role_id=None,
    )
    skill = CodexJobResultSkill()
    result = await skill.run(ctx=ctx, args={"job_id": j.id})
    assert result.payload["status"] == "failed"
    assert "codex auth expired" in result.payload["error"]
    job_registry.clear()
