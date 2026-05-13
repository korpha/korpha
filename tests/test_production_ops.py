"""Tests for the production-ops upgrades:
   * /healthz reports DB ping + version + uptime
   * korpha backup / restore round-trips
   * observability.report_error dispatches to active reporter
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from typer.testing import CliRunner

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.observability import (
    report_error, reset_error_reporter, set_error_reporter,
)


# ---- /healthz ----


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        f = Founder(email="x@y.com", display_name="Mike")
        s.add(f); s.commit(); s.refresh(f)
        b = Business(
            founder_id=f.id, name="WidgetCo", description="t",
        )
        s.add(b); s.commit()
    from korpha.api.server import build_app
    return TestClient(build_app()), tmp_path


def test_healthz_ok_with_db(http) -> None:
    client, _ = http
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_reachable"] is True
    assert body["version"] is not None
    assert body["uptime_seconds"] > 0
    assert body["skills_loaded"] > 0


def test_healthz_reports_provider_state(http) -> None:
    client, _ = http
    r = client.get("/healthz")
    body = r.json()
    # has_provider depends on env vars; just verify the field exists
    # and is a bool.
    assert isinstance(body["has_provider"], bool)


def test_healthz_degraded_when_db_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point at a non-existent data dir → healthz returns degraded."""
    # Server's _build_engine raises HTTPException 503 when the data
    # dir doesn't exist; healthz catches that and reports degraded.
    monkeypatch.setenv(
        "KORPHA_DATA_DIR", str(tmp_path / "does-not-exist"),
    )
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    from korpha.api.server import build_app
    client = TestClient(build_app())
    r = client.get("/healthz")
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db_reachable"] is False


# ---- backup / restore ----


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Init-style data dir: db + a couple subdirs to verify they're
    captured in the tarball."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KORPHA_DATA_DIR", str(data_dir))
    db_path = data_dir / "korpha.db"
    monkeypatch.setenv("KORPHA_DB_URL", f"sqlite:///{db_path}")
    from korpha.db._session import get_engine
    get_engine.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    # Write some artifacts
    (data_dir / "skills").mkdir()
    (data_dir / "skills" / "marker.txt").write_text("agent skill")
    (data_dir / "cron-scripts").mkdir()
    (data_dir / "cron-scripts" / "watch.sh").write_text("#!/bin/sh\necho ok")
    return CliRunner(), tmp_path


def test_backup_creates_tarball(cli) -> None:
    cli_runner, tmp = cli
    output = tmp / "out.tar.gz"
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "backup", "--output", str(output),
    ])
    assert result.exit_code == 0, result.stdout
    assert output.is_file()
    assert output.stat().st_size > 0
    # Tarball is parseable + contains expected entries
    import tarfile
    with tarfile.open(output, "r:gz") as tar:
        names = tar.getnames()
    assert any("korpha/korpha.db" in n for n in names)
    assert any("korpha/skills/marker.txt" in n for n in names)


def test_backup_no_data_dir_fails_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path / "missing"))
    from korpha.cli import app
    result = CliRunner().invoke(app, ["backup"])
    assert result.exit_code == 1
    assert "init" in result.stdout.lower()


def test_restore_into_clean_dir(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_runner, _ = cli
    # Step 1: backup the existing fixture
    output = tmp_path / "snap.tar.gz"
    from korpha.cli import app
    cli_runner.invoke(app, ["backup", "--output", str(output)])
    assert output.is_file()

    # Step 2: point at a fresh empty data dir + restore
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("KORPHA_DATA_DIR", str(fresh))
    from korpha.db._session import get_engine
    get_engine.cache_clear()

    result = cli_runner.invoke(app, ["restore", str(output)])
    assert result.exit_code == 0, result.stdout
    assert (fresh / "korpha.db").is_file()
    assert (fresh / "skills" / "marker.txt").read_text() == "agent skill"


def test_restore_refuses_nonempty_without_force(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_runner, _ = cli
    output = tmp_path / "snap.tar.gz"
    from korpha.cli import app
    cli_runner.invoke(app, ["backup", "--output", str(output)])

    # Restore back into the same (now non-empty) dir
    result = cli_runner.invoke(app, ["restore", str(output)])
    assert result.exit_code == 1
    assert "force" in result.stdout.lower()


def test_restore_force_clobbers(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_runner, _ = cli
    output = tmp_path / "snap.tar.gz"
    from korpha.cli import app
    cli_runner.invoke(app, ["backup", "--output", str(output)])
    result = cli_runner.invoke(app, [
        "restore", str(output), "--force",
    ])
    assert result.exit_code == 0


def test_restore_missing_tarball_fails(tmp_path: Path) -> None:
    from korpha.cli import app
    result = CliRunner().invoke(app, [
        "restore", str(tmp_path / "nope.tar.gz"),
    ])
    assert result.exit_code == 1
    assert "not found" in result.stdout.lower()


# ---- observability ----


def test_default_reporter_logs_via_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default reporter goes through ``logger.exception`` so
    any logging sink picks it up."""
    import logging as _lg
    caplog.set_level(_lg.ERROR, logger="korpha.observability")
    try:
        raise ValueError("a thing happened")
    except ValueError as exc:
        report_error(exc, context={"skill": "test"})
    assert any(
        "ValueError" in r.message for r in caplog.records
    )


def test_custom_reporter_replaces_default() -> None:
    captured: list[tuple[BaseException, dict]] = []

    def my_reporter(exc, ctx):
        captured.append((exc, ctx))

    set_error_reporter(my_reporter)
    try:
        try:
            raise RuntimeError("custom path")
        except RuntimeError as exc:
            report_error(exc, context={"x": 1})
    finally:
        reset_error_reporter()

    assert len(captured) == 1
    assert isinstance(captured[0][0], RuntimeError)
    assert captured[0][1] == {"x": 1}


def test_reporter_failure_falls_back_to_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A buggy reporter must not lose the original error."""
    import logging as _lg

    def bad_reporter(exc, ctx):
        raise RuntimeError("reporter is broken")

    set_error_reporter(bad_reporter)
    caplog.set_level(_lg.ERROR, logger="korpha.observability")
    try:
        try:
            raise ValueError("the original error")
        except ValueError as exc:
            report_error(exc)
    finally:
        reset_error_reporter()
    # Original error message landed in logging
    assert any(
        "ValueError" in r.message for r in caplog.records
    )


def test_set_error_reporter_returns_previous() -> None:
    def first(exc, ctx): pass
    def second(exc, ctx): pass

    prev = set_error_reporter(first)
    assert prev is not first

    prev2 = set_error_reporter(second)
    assert prev2 is first
    reset_error_reporter()


# ---- first-run hint ----


def test_first_run_hint_shown_when_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path / "fresh"))
    from korpha.cli import app
    result = CliRunner().invoke(app, [])
    assert "First time?" in result.stdout
    assert "korpha init" in result.stdout


def test_first_run_hint_omitted_when_db_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    (tmp_path / "korpha.db").write_text("")
    from korpha.cli import app
    result = CliRunner().invoke(app, [])
    assert "First time?" not in result.stdout
