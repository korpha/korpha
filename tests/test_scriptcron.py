"""Tests for the agentless script cron runner.

Covers cadence parsing, due-detection, the four delivery branches
(success-with-output / success-empty / non-zero-exit / hard-fail),
and the multi-tenant scoping on run_due_jobs.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.scriptcron import (
    ScriptCron, ScriptCronStatus, parse_cadence, run_due_jobs, run_job,
)


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path}/cron.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session) -> UUID:
    f = Founder(email="x@y.com", display_name="Mike")
    session.add(f); session.commit(); session.refresh(f)
    b = Business(
        founder_id=f.id, name="WidgetCo", description="t",
    )
    session.add(b); session.commit(); session.refresh(b)
    return b.id


def _write_script(
    tmp_path: Path, name: str, body: str, ext: str = ".sh",
) -> Path:
    path = tmp_path / f"{name}{ext}"
    path.write_text(body)
    path.chmod(0o755)
    return path


# ---- parse_cadence ----


@pytest.mark.parametrize("text,expected", [
    ("every 5m", timedelta(minutes=5)),
    ("every 1h", timedelta(hours=1)),
    ("every 12h", timedelta(hours=12)),
    ("every 7d", timedelta(days=7)),
    ("EVERY 30 MIN", timedelta(minutes=30)),
    ("every 2 hours", timedelta(hours=2)),
])
def test_parse_cadence_accepts_known_shapes(
    text: str, expected: timedelta,
) -> None:
    assert parse_cadence(text) == expected


@pytest.mark.parametrize("bad", [
    "", "5m", "now", "every -5m", "every 0h", "every 5x",
])
def test_parse_cadence_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_cadence(bad)


# ---- run_job: success path with output ----


@pytest.mark.asyncio
async def test_run_job_delivers_stdout_when_output(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script prints something → it's pushed verbatim to the
    configured channel + status=ok."""
    biz = _seed(session)
    script = _write_script(
        tmp_path, "watchdog",
        "#!/bin/bash\necho 'memory at 78%'\n",
    )
    job = ScriptCron(
        business_id=biz, name="memory-watch",
        script_path=str(script), cadence="every 5m",
        deliver_platform="email",
        deliver_recipient="mike@x.com",
    )
    session.add(job); session.commit(); session.refresh(job)

    sent: list[dict] = []

    async def _stub_email(*, recipient, content, subject):
        sent.append({
            "recipient": recipient,
            "content": content,
            "subject": subject,
        })
        return {"to": recipient}

    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("RESEND_API_KEY",), _stub_email)},
    )

    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.OK
    assert outcome.delivered is True
    assert outcome.exit_code == 0
    assert "memory at 78%" in outcome.stdout
    assert sent[0]["content"] == "memory at 78%"
    assert "memory-watch" in sent[0]["subject"]
    # Persisted
    session.refresh(job)
    assert job.last_status == ScriptCronStatus.OK
    assert job.last_run_at is not None


# ---- run_job: silent (watchdog) path ----


@pytest.mark.asyncio
async def test_run_job_silent_when_no_stdout(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog pattern: success + no output → no delivery, status=silent.
    'No news is good news' — don't spam the founder every 5 min."""
    biz = _seed(session)
    script = _write_script(tmp_path, "quiet", "#!/bin/bash\nexit 0\n")
    job = ScriptCron(
        business_id=biz, name="ok",
        script_path=str(script), cadence="every 5m",
        deliver_platform="email",
        deliver_recipient="mike@x.com",
    )
    session.add(job); session.commit(); session.refresh(job)

    called: list[dict] = []

    async def _spy(*, recipient, content, subject):
        called.append({"r": recipient})
        return {"to": recipient}

    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("X",), _spy)},
    )

    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.SILENT
    assert outcome.delivered is False
    assert called == []  # NO push


# ---- run_job: failure paths ----


@pytest.mark.asyncio
async def test_run_job_alerts_on_non_zero_exit(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero exit → ❌ alert, status=failed. Broken watchdog
    cannot fail silently."""
    biz = _seed(session)
    script = _write_script(
        tmp_path, "broken",
        "#!/bin/bash\necho 'partial' >&2\nexit 7\n",
    )
    job = ScriptCron(
        business_id=biz, name="bad-cron",
        script_path=str(script), cadence="every 5m",
        deliver_platform="email",
        deliver_recipient="mike@x.com",
    )
    session.add(job); session.commit(); session.refresh(job)

    sent: list[str] = []

    async def _stub(*, recipient, content, subject):
        sent.append(content)
        return {"to": recipient}

    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("X",), _stub)},
    )

    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.FAILED
    assert outcome.exit_code == 7
    assert outcome.delivered is True
    assert "❌" in sent[0]
    assert "exited 7" in sent[0]
    assert "partial" in sent[0]  # stderr surfaced


@pytest.mark.asyncio
async def test_run_job_alerts_when_script_missing(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    biz = _seed(session)
    job = ScriptCron(
        business_id=biz, name="missing",
        script_path=str(tmp_path / "nope.sh"),
        cadence="every 5m",
        deliver_platform="email",
        deliver_recipient="mike@x.com",
    )
    session.add(job); session.commit(); session.refresh(job)

    sent: list[str] = []

    async def _stub(*, recipient, content, subject):
        sent.append(content)
        return {"to": recipient}

    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("X",), _stub)},
    )

    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.FAILED
    assert "not found" in outcome.error
    assert sent and "❌" in sent[0]


@pytest.mark.asyncio
async def test_run_job_timeout_kills_script(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long-running script hits the timeout → killed + alert."""
    biz = _seed(session)
    script = _write_script(
        tmp_path, "slow",
        "#!/bin/bash\nsleep 30\n",
    )
    job = ScriptCron(
        business_id=biz, name="slow",
        script_path=str(script), cadence="every 5m",
        deliver_platform="email",
        deliver_recipient="mike@x.com",
    )
    session.add(job); session.commit(); session.refresh(job)

    sent: list[str] = []

    async def _stub(*, recipient, content, subject):
        sent.append(content)
        return {"to": recipient}

    monkeypatch.setattr(
        "korpha.skills.channel._PLATFORM_SENDERS",
        {"email": (("X",), _stub)},
    )

    outcome = await run_job(
        session, job, timeout_seconds=0.2,
    )
    assert outcome.status == ScriptCronStatus.FAILED
    assert "timeout" in outcome.error.lower()
    assert sent and "timeout" in sent[0].lower()


# ---- run_job: log-only mode (no delivery configured) ----


@pytest.mark.asyncio
async def test_run_job_no_delivery_when_no_channel_set(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed(session)
    script = _write_script(
        tmp_path, "echo",
        "#!/bin/bash\necho 'hello'\n",
    )
    job = ScriptCron(
        business_id=biz, name="log-only",
        script_path=str(script), cadence="every 5m",
        deliver_platform=None, deliver_recipient=None,
    )
    session.add(job); session.commit(); session.refresh(job)
    outcome = await run_job(session, job)
    assert outcome.status == ScriptCronStatus.OK
    assert outcome.delivered is False
    # last_output persisted for the dashboard to show
    session.refresh(job)
    assert "hello" in job.last_output


# ---- run_due_jobs ----


@pytest.mark.asyncio
async def test_run_due_jobs_runs_only_due_ones(
    session: Session, tmp_path: Path,
) -> None:
    """A job that ran 1 min ago with cadence 'every 5m' is NOT due;
    one that's never run IS due."""
    biz = _seed(session)
    script = _write_script(tmp_path, "x", "#!/bin/bash\nexit 0\n")

    not_due = ScriptCron(
        business_id=biz, name="recent",
        script_path=str(script), cadence="every 5m",
        last_run_at=datetime.now(tz=timezone.utc) - timedelta(seconds=30),
    )
    due = ScriptCron(
        business_id=biz, name="never-run",
        script_path=str(script), cadence="every 5m",
    )
    session.add(not_due); session.add(due); session.commit()

    outcomes = await run_due_jobs(session)
    ran = {o.job_id for o in outcomes}
    assert due.id in ran
    assert not_due.id not in ran


@pytest.mark.asyncio
async def test_run_due_jobs_skips_disabled(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed(session)
    script = _write_script(tmp_path, "x", "#!/bin/bash\nexit 0\n")
    job = ScriptCron(
        business_id=biz, name="off",
        script_path=str(script), cadence="every 5m",
        enabled=False,
    )
    session.add(job); session.commit()
    outcomes = await run_due_jobs(session)
    assert outcomes == []


@pytest.mark.asyncio
async def test_run_due_jobs_filters_by_business(
    session: Session, tmp_path: Path,
) -> None:
    """Multi-tenant: business_id arg scopes."""
    biz_a = _seed(session)
    f = Founder(email="b@y.com", display_name="B")
    session.add(f); session.commit(); session.refresh(f)
    biz_b = Business(founder_id=f.id, name="B", description="")
    session.add(biz_b); session.commit(); session.refresh(biz_b)

    script = _write_script(tmp_path, "x", "#!/bin/bash\nexit 0\n")
    job_a = ScriptCron(
        business_id=biz_a, name="a-job",
        script_path=str(script), cadence="every 5m",
    )
    job_b = ScriptCron(
        business_id=biz_b.id, name="b-job",
        script_path=str(script), cadence="every 5m",
    )
    session.add(job_a); session.add(job_b); session.commit()

    outcomes = await run_due_jobs(session, business_id=biz_a)
    assert len(outcomes) == 1
    assert outcomes[0].job_id == job_a.id


@pytest.mark.asyncio
async def test_run_due_jobs_one_failure_does_not_poison_loop(
    session: Session, tmp_path: Path,
) -> None:
    """If one job's runner raises (broken interpreter, OS error),
    the remaining jobs still execute."""
    biz = _seed(session)
    good = _write_script(
        tmp_path, "good", "#!/bin/bash\necho ok\n",
    )
    job_good = ScriptCron(
        business_id=biz, name="good",
        script_path=str(good), cadence="every 5m",
    )
    job_bad = ScriptCron(
        business_id=biz, name="bad",
        script_path=str(tmp_path / "missing.sh"),
        cadence="every 5m",
    )
    session.add(job_good); session.add(job_bad); session.commit()

    outcomes = await run_due_jobs(session)
    # Both ran; bad is FAILED, good is OK
    by_name = {
        o.job_id: o.status
        for o in outcomes
    }
    session.refresh(job_good)
    session.refresh(job_bad)
    assert job_good.last_status == ScriptCronStatus.OK
    assert job_bad.last_status == ScriptCronStatus.FAILED


# ---- delivery skip on unknown channel ----


@pytest.mark.asyncio
async def test_run_job_logs_warning_for_unknown_channel(
    session: Session, tmp_path: Path,
) -> None:
    biz = _seed(session)
    script = _write_script(
        tmp_path, "x", "#!/bin/bash\necho hi\n",
    )
    job = ScriptCron(
        business_id=biz, name="x",
        script_path=str(script), cadence="every 5m",
        deliver_platform="not-a-platform",
        deliver_recipient="x@y.com",
    )
    session.add(job); session.commit(); session.refresh(job)
    outcome = await run_job(session, job)
    # Status still OK (script ran fine); just no delivery
    assert outcome.status == ScriptCronStatus.OK
    assert outcome.delivered is False
