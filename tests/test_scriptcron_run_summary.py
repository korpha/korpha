"""Tests for the heartbeat run-summary digest on ScriptCron."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.scriptcron import run_job
from korpha.scriptcron.model import (
    ScriptCron, ScriptCronStatus,
)
from korpha.scriptcron.runner import _build_run_summary


# ---- _build_run_summary unit tests ----


def test_summary_silent_status() -> None:
    s = _build_run_summary(
        status=ScriptCronStatus.SILENT,
        stdout="", stderr="", error="", exit_code=0,
        delivered=False,
    )
    assert "silent" in s.lower()
    assert s.startswith("✓")


def test_summary_ok_uses_first_stdout_line() -> None:
    s = _build_run_summary(
        status=ScriptCronStatus.OK,
        stdout="✓ memory at 78%\nmore detail\nthird",
        stderr="", error="", exit_code=0, delivered=True,
    )
    assert "memory at 78%" in s
    assert "delivered" in s
    assert s.startswith("✓")


def test_summary_ok_skips_blank_first_lines() -> None:
    s = _build_run_summary(
        status=ScriptCronStatus.OK,
        stdout="\n\n\nfirst real line\n",
        stderr="", error="", exit_code=0, delivered=True,
    )
    assert "first real line" in s


def test_summary_failed_prefers_stderr() -> None:
    s = _build_run_summary(
        status=ScriptCronStatus.FAILED,
        stdout="some output", stderr="oh no\ntraceback",
        error="", exit_code=1, delivered=True,
    )
    assert s.startswith("✗")
    assert "oh no" in s
    assert "exit 1" in s


def test_summary_failed_falls_back_to_error_when_no_stderr() -> None:
    s = _build_run_summary(
        status=ScriptCronStatus.FAILED,
        stdout="", stderr="", error="timeout after 30s",
        exit_code=124, delivered=True,
    )
    assert "timeout" in s
    assert "exit 124" in s


def test_summary_truncates_long_headlines() -> None:
    long_line = "x" * 500
    s = _build_run_summary(
        status=ScriptCronStatus.OK,
        stdout=long_line, stderr="", error="",
        exit_code=0, delivered=True,
    )
    # Bounded at 400 chars total
    assert len(s) <= 400


def test_summary_ok_no_delivery_omits_delivered_pill() -> None:
    s = _build_run_summary(
        status=ScriptCronStatus.OK,
        stdout="something", stderr="", error="",
        exit_code=0, delivered=False,
    )
    assert "delivered" not in s


def test_summary_zero_exit_doesnt_show_exit_code() -> None:
    s = _build_run_summary(
        status=ScriptCronStatus.OK,
        stdout="ok", stderr="", error="",
        exit_code=0, delivered=False,
    )
    assert "exit" not in s


# ---- end-to-end run_job populates last_summary ----


@pytest.fixture
def echo_script(tmp_path: Path) -> Path:
    """A trivially-OK script that prints a known headline."""
    p = tmp_path / "s.sh"
    p.write_text(
        "#!/bin/bash\necho 'hello from cron'\n",
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


@pytest.fixture
def fail_script(tmp_path: Path) -> Path:
    p = tmp_path / "fail.sh"
    p.write_text(
        "#!/bin/bash\n>&2 echo 'boom'\nexit 7\n",
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


@pytest.fixture
def silent_script(tmp_path: Path) -> Path:
    p = tmp_path / "silent.sh"
    p.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    p.chmod(0o755)
    return p


@pytest.mark.asyncio
async def test_run_job_populates_summary_for_ok(
    session: Session, business: Business, echo_script: Path,
) -> None:
    job = ScriptCron(
        business_id=business.id, name="t",
        script_path=str(echo_script),
        cadence="every 1m",
    )
    session.add(job); session.commit(); session.refresh(job)
    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.OK
    session.refresh(job)
    assert job.last_summary
    assert "hello from cron" in job.last_summary


@pytest.mark.asyncio
async def test_run_job_populates_summary_for_failed(
    session: Session, business: Business, fail_script: Path,
) -> None:
    job = ScriptCron(
        business_id=business.id, name="t",
        script_path=str(fail_script),
        cadence="every 1m",
    )
    session.add(job); session.commit(); session.refresh(job)
    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.FAILED
    session.refresh(job)
    assert job.last_summary
    assert "✗" in job.last_summary
    assert "boom" in job.last_summary
    assert "exit 7" in job.last_summary


@pytest.mark.asyncio
async def test_run_job_populates_summary_for_silent(
    session: Session, business: Business, silent_script: Path,
) -> None:
    job = ScriptCron(
        business_id=business.id, name="t",
        script_path=str(silent_script),
        cadence="every 1m",
    )
    session.add(job); session.commit(); session.refresh(job)
    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.SILENT
    session.refresh(job)
    assert "silent" in job.last_summary.lower()
