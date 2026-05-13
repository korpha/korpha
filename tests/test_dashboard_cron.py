"""Tests for the dashboard cron panel."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from korpha.business.model import Business
from korpha.cofounder.model import AgentRole, RoleType
from korpha.identity.model import Founder
from korpha.scriptcron.model import ScriptCron, ScriptCronStatus  # noqa: F401


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


def _add_cron(
    data_dir: Path, business_id: UUID, *,
    name: str, script_path: str = "/bin/true",
    cadence: str = "every 5m",
) -> UUID:
    db_path = data_dir / "korpha.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        from uuid import uuid4 as _uuid4
        job = ScriptCron(
            id=_uuid4(),
            business_id=business_id, name=name,
            script_path=script_path, cadence=cadence,
        )
        s.add(job); s.commit()
        return job.id


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    business_id = _seed(tmp_path)
    from korpha.api.server import build_app
    return TestClient(build_app()), business_id, tmp_path


# ---- list ----


def test_cron_page_renders_empty_state(http) -> None:
    client, _, _ = http
    r = client.get("/app/cron")
    assert r.status_code == 200
    assert "No cron jobs yet" in r.text
    assert "korpha cron add" in r.text  # CTA shows the CLI


def test_cron_page_lists_jobs(http) -> None:
    client, biz, tmp = http
    _add_cron(tmp, biz, name="memory-watch")
    _add_cron(tmp, biz, name="rss-pull", cadence="every 12h")
    r = client.get("/app/cron")
    assert r.status_code == 200
    assert "memory-watch" in r.text
    assert "rss-pull" in r.text
    assert "every 12h" in r.text


def test_cron_page_isolates_by_business(http) -> None:
    client, biz, tmp = http
    _add_cron(tmp, biz, name="ours")
    # Other business's cron must not appear
    _add_cron(tmp, uuid4(), name="theirs")
    r = client.get("/app/cron")
    assert "ours" in r.text
    assert "theirs" not in r.text


def test_cron_page_shows_status_pill(http) -> None:
    """Last status renders as a colored pill."""
    client, biz, tmp = http
    cid = _add_cron(tmp, biz, name="t")
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        job = s.get(ScriptCron, cid)
        job.last_status = ScriptCronStatus.OK
        job.last_output = "memory at 78%"
        s.add(job); s.commit()
    r = client.get("/app/cron")
    assert "cron-pill-ok" in r.text
    assert "memory at 78%" in r.text  # last output shown


# ---- run-now ----


def test_run_now_post_redirects_with_status(http) -> None:
    client, biz, tmp = http
    cid = _add_cron(
        tmp, biz, name="quick", script_path="/bin/true",
    )
    r = client.post(
        f"/app/cron/{cid}/run-now", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ran=quick" in r.headers["location"]


def test_run_now_unknown_job_returns_not_found(http) -> None:
    client, _, _ = http
    r = client.post(
        f"/app/cron/{uuid4()}/run-now", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=not_found" in r.headers["location"]


def test_run_now_other_business_refused(http) -> None:
    """Multi-tenant: founder A can't run founder B's cron."""
    client, _, tmp = http
    other = _add_cron(tmp, uuid4(), name="theirs")
    r = client.post(
        f"/app/cron/{other}/run-now", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=not_found" in r.headers["location"]


# ---- toggle ----


def test_toggle_flips_enabled(http) -> None:
    client, biz, tmp = http
    cid = _add_cron(tmp, biz, name="x")
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    # Currently enabled
    with Session(engine) as s:
        assert s.get(ScriptCron, cid).enabled is True
    r = client.post(
        f"/app/cron/{cid}/toggle", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "toggled=off" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(ScriptCron, cid).enabled is False
    # Toggle back on
    client.post(f"/app/cron/{cid}/toggle", follow_redirects=False)
    with Session(engine) as s:
        assert s.get(ScriptCron, cid).enabled is True


# ---- delete ----


def test_delete_removes_job(http) -> None:
    client, biz, tmp = http
    cid = _add_cron(tmp, biz, name="tmp")
    r = client.post(
        f"/app/cron/{cid}/delete", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "deleted=tmp" in r.headers["location"]
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        assert s.get(ScriptCron, cid) is None


def test_delete_other_business_refused(http) -> None:
    client, _, tmp = http
    other = _add_cron(tmp, uuid4(), name="theirs")
    r = client.post(
        f"/app/cron/{other}/delete", follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=not_found" in r.headers["location"]
    # Row still exists
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        assert s.get(ScriptCron, other) is not None


# ---- bad ids ----


def test_bad_id_returns_bad_id_redirect(http) -> None:
    client, _, _ = http
    for verb in ("run-now", "toggle", "delete"):
        r = client.post(
            f"/app/cron/not-a-uuid/{verb}", follow_redirects=False,
        )
        assert r.status_code == 303
        assert "error=bad_id" in r.headers["location"]


# ---- create form ----


def test_new_form_renders(http) -> None:
    """GET /app/cron/new shows the form with default field values."""
    client, _, _ = http
    r = client.get("/app/cron/new")
    assert r.status_code == 200
    assert 'name="name"' in r.text
    assert 'name="cadence"' in r.text
    assert 'name="script_content"' in r.text
    assert 'name="extension"' in r.text
    assert 'name="deliver"' in r.text
    assert 'name="recipient"' in r.text
    # Default cadence shown
    assert "every 1h" in r.text


def test_new_post_creates_job_and_redirects(http) -> None:
    client, biz, tmp = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "memory-watch",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "#!/bin/bash\nfree -m\n",
            "deliver": "email",
            "recipient": "mike@example.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "created=memory-watch" in r.headers["location"]
    # Row in DB
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        rows = list(s.exec(
            select(ScriptCron).where(ScriptCron.name == "memory-watch"),
        ).all())
    assert len(rows) == 1
    assert rows[0].deliver_platform == "email"
    # Script written to disk
    assert Path(rows[0].script_path).exists()
    assert "free -m" in Path(rows[0].script_path).read_text()


def test_new_post_log_only_mode(http) -> None:
    """Empty deliver + recipient should be accepted as log-only."""
    client, _, tmp = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "logger",
            "extension": ".sh",
            "cadence": "every 1h",
            "script_content": "echo done",
            "deliver": "",
            "recipient": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        row = s.exec(
            select(ScriptCron).where(ScriptCron.name == "logger"),
        ).first()
    assert row is not None
    assert row.deliver_platform is None
    assert row.deliver_recipient is None


def test_new_post_rejects_bad_name(http) -> None:
    client, _, _ = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "../escape",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "echo hi",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Name must be" in r.text


def test_new_post_rejects_bad_cadence(http) -> None:
    client, _, _ = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "x",
            "extension": ".sh",
            "cadence": "soon",
            "script_content": "echo hi",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "cadence" in r.text.lower()


def test_new_post_rejects_empty_script(http) -> None:
    client, _, _ = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "x",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "  ",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Script body" in r.text


def test_new_post_rejects_unknown_extension(http) -> None:
    client, _, _ = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "x",
            "extension": ".rb",
            "cadence": "every 5m",
            "script_content": "puts 'hi'",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Extension" in r.text


def test_new_post_rejects_unknown_deliver(http) -> None:
    client, _, _ = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "x",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "echo hi",
            "deliver": "fax",
            "recipient": "555-1234",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Deliver" in r.text


def test_new_post_requires_recipient_with_deliver(http) -> None:
    client, _, _ = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "x",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "echo hi",
            "deliver": "email",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Recipient is required" in r.text


def test_new_post_requires_deliver_when_recipient_set(http) -> None:
    client, _, _ = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "x",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "echo hi",
            "recipient": "mike@x.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Delivery channel" in r.text


def test_new_post_rejects_dangerous_script(http) -> None:
    """Safety scanner refuses obvious wreckage even from the form."""
    client, _, tmp = http
    r = client.post(
        "/app/cron/new",
        data={
            "name": "evil",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "#!/bin/bash\nrm -rf /\n",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "safety scan" in r.text.lower()
    # No row created
    db = tmp / "korpha.db"
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        assert s.exec(
            select(ScriptCron).where(ScriptCron.name == "evil"),
        ).first() is None


def test_new_post_rejects_duplicate_name(http) -> None:
    client, biz, tmp = http
    _add_cron(tmp, biz, name="dup")
    r = client.post(
        "/app/cron/new",
        data={
            "name": "dup",
            "extension": ".sh",
            "cadence": "every 5m",
            "script_content": "echo hi",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "already exists" in r.text


def test_cron_list_shows_new_button(http) -> None:
    """The list page links to /app/cron/new for the form."""
    client, _, _ = http
    r = client.get("/app/cron")
    assert r.status_code == 200
    assert 'href="/app/cron/new"' in r.text


def test_created_flash_renders_on_redirect_target(http) -> None:
    client, _, _ = http
    r = client.get("/app/cron?created=memory-watch")
    assert r.status_code == 200
    assert "Created cron" in r.text
    assert "memory-watch" in r.text


# ---- nav ----


def test_nav_includes_cron_link(http) -> None:
    client, _, _ = http
    r = client.get("/app/cron")
    assert r.status_code == 200
    assert 'href="/app/cron"' in r.text
    assert 'is-active' in r.text
