"""Tests for the dashboard jobs panel."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder


def _seed(data_dir: Path) -> UUID:
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo",
            description="t", founder_brief={"goal": "t"},
        )
        s.add(b); s.commit(); s.refresh(b)
        s.add(AgentRole(
            business_id=b.id, role_type=RoleType.CEO, title="CEO",
        ))
        s.commit()
        return b.id


@pytest.fixture
def http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, UUID]:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    business_id = _seed(tmp_path)
    # Reset job registry between tests so prior runs don't leak in
    from korpha.jobs import job_registry
    job_registry.clear()
    from korpha.api.server import build_app
    yield TestClient(build_app()), business_id
    job_registry.clear()


# ---- list ----


def test_jobs_page_renders_empty_state(
    http: tuple[TestClient, UUID],
) -> None:
    client, _ = http
    r = client.get("/app/jobs")
    assert r.status_code == 200
    assert "No background jobs" in r.text


def test_jobs_page_lists_running_jobs(
    http: tuple[TestClient, UUID],
) -> None:
    """A job submitted for this business renders with Cancel button."""
    client, business_id = http
    from korpha.jobs import job_registry

    async def _slow():
        await asyncio.sleep(60)
        return {"x": 1}

    # We need an event loop to submit the job — use asyncio.new_event_loop
    import asyncio as _aio
    loop = _aio.new_event_loop()
    try:
        async def _go():
            return job_registry.submit(
                _slow(), label="ship_via_codex: refactor",
                business_id=str(business_id),
            )
        job = loop.run_until_complete(_go())

        r = client.get("/app/jobs")
        assert r.status_code == 200
        assert job.id in r.text
        assert "ship_via_codex: refactor" in r.text
        assert "running" in r.text
        # Cancel button present for running jobs
        assert f"/app/jobs/{job.id}/cancel" in r.text
    finally:
        # Cancel + drain so the loop doesn't have orphan tasks
        loop.run_until_complete(_drain(job_registry, job.id))
        loop.close()


async def _drain(reg, jid: str) -> None:
    reg.cancel(jid)
    j = reg.get(jid)
    if j is None or j._task is None:
        return
    try:
        await j._task
    except BaseException:
        pass


def test_jobs_page_filters_by_business(
    http: tuple[TestClient, UUID],
) -> None:
    """A job submitted for a DIFFERENT business doesn't appear."""
    client, _ = http
    from korpha.jobs import job_registry
    import asyncio as _aio

    loop = _aio.new_event_loop()
    try:
        async def _go():
            async def _ok(): return "x"
            return job_registry.submit(
                _ok(), label="other-biz",
                business_id="other-biz-id",
            )
        loop.run_until_complete(_go())
        # Drain
        loop.run_until_complete(_aio.sleep(0))
    finally:
        loop.close()

    r = client.get("/app/jobs")
    assert r.status_code == 200
    assert "other-biz" not in r.text


def test_jobs_page_renders_completed_with_result(
    http: tuple[TestClient, UUID],
) -> None:
    """Completed jobs appear with status pill and no Cancel button."""
    client, business_id = http
    from korpha.jobs import Job, JobStatus, job_registry

    j = Job(
        id="abc12345", label="ship: done",
        business_id=str(business_id),
    )
    j.status = JobStatus.COMPLETED
    j.started_at = time.time() - 5.0
    j.finished_at = time.time()
    j.result = {"content": "x"}
    job_registry._jobs[j.id] = j

    r = client.get("/app/jobs")
    assert r.status_code == 200
    assert "abc12345" in r.text
    assert "completed" in r.text
    # No cancel form for terminal jobs
    assert "/app/jobs/abc12345/cancel" not in r.text


def test_jobs_page_shows_error_for_failed_job(
    http: tuple[TestClient, UUID],
) -> None:
    client, business_id = http
    from korpha.jobs import Job, JobStatus, job_registry

    j = Job(
        id="bad12345", label="ship: oops",
        business_id=str(business_id),
    )
    j.status = JobStatus.FAILED
    j.started_at = time.time() - 2.0
    j.finished_at = time.time()
    j.error = "RuntimeError: codex auth expired"
    job_registry._jobs[j.id] = j

    r = client.get("/app/jobs")
    assert r.status_code == 200
    assert "bad12345" in r.text
    assert "failed" in r.text
    assert "auth expired" in r.text


# ---- cancel ----


def test_cancel_running_job(
    http: tuple[TestClient, UUID],
) -> None:
    client, business_id = http
    from korpha.jobs import job_registry
    import asyncio as _aio

    loop = _aio.new_event_loop()
    try:
        async def _slow():
            await _aio.sleep(60)
            return "x"
        async def _go():
            return job_registry.submit(
                _slow(), label="t",
                business_id=str(business_id),
            )
        job = loop.run_until_complete(_go())

        r = client.post(
            f"/app/jobs/{job.id}/cancel", follow_redirects=False,
        )
        assert r.status_code == 303
        assert "cancelled=1" in r.headers["location"]
    finally:
        loop.run_until_complete(_drain(job_registry, job.id))
        loop.close()


def test_cancel_unknown_job_returns_not_found(
    http: tuple[TestClient, UUID],
) -> None:
    client, _ = http
    r = client.post(
        "/app/jobs/nonexistent/cancel", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=not_found" in r.headers["location"]


def test_cancel_other_business_job_returns_not_found(
    http: tuple[TestClient, UUID],
) -> None:
    """Multi-tenant safety — founder of A can't cancel B's job."""
    client, _ = http
    from korpha.jobs import Job, JobStatus, job_registry

    j = Job(
        id="other123", label="other-biz",
        business_id="other-business-id",
    )
    j.status = JobStatus.RUNNING
    j.started_at = time.time()
    job_registry._jobs[j.id] = j

    r = client.post(
        f"/app/jobs/{j.id}/cancel", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=not_found" in r.headers["location"]
    # Job is unchanged
    assert job_registry.get("other123").status == JobStatus.RUNNING


# ---- nav ----


def test_nav_includes_jobs_link(
    http: tuple[TestClient, UUID],
) -> None:
    client, _ = http
    r = client.get("/app/jobs")
    assert r.status_code == 200
    assert 'href="/app/jobs"' in r.text
    assert 'is-active' in r.text
