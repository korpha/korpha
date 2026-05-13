"""Tests for `korpha cron add-healthcheck` preset."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from typer.testing import CliRunner

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.scriptcron.model import ScriptCron  # noqa: F401


@pytest.fixture
def runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, Path]:
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
    return CliRunner(), tmp_path


def _read_jobs(tmp_path: Path) -> list[ScriptCron]:
    engine = create_engine(f"sqlite:///{tmp_path / 'korpha.db'}")
    with Session(engine) as s:
        return list(s.exec(select(ScriptCron)).all())


def test_add_healthcheck_creates_job(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    # Use a public IP literal so SSRF gate passes without DNS
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/",
        "--every", "every 1h",
    ])
    assert result.exit_code == 0, result.stdout
    assert "Healthcheck cron added" in result.stdout
    jobs = _read_jobs(tmp)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.cadence == "every 1h"
    assert "healthcheck-" in job.name  # derived from hostname
    body = Path(job.script_path).read_text()
    assert "https://8.8.8.8/" in body
    assert "curl" in body


def test_add_healthcheck_derives_name_from_hostname(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/health",
    ])
    assert result.exit_code == 0
    jobs = _read_jobs(tmp)
    assert jobs[0].name == "healthcheck-8-8-8-8"


def test_add_healthcheck_with_delivery(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/",
        "--deliver", "email",
        "--to", "ops@example.com",
        "--timeout", "30",
    ])
    assert result.exit_code == 0
    job = _read_jobs(tmp)[0]
    assert job.deliver_platform == "email"
    assert job.deliver_recipient == "ops@example.com"
    body = Path(job.script_path).read_text()
    assert "TIMEOUT=30" in body


def test_add_healthcheck_custom_name(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/",
        "--name", "prod-api",
    ])
    assert result.exit_code == 0
    jobs = _read_jobs(tmp)
    assert jobs[0].name == "prod-api"


def test_add_healthcheck_rejects_metadata_url(runner) -> None:
    """SSRF gate fires — Mike can't accidentally watchdog the
    cloud metadata endpoint."""
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "http://169.254.169.254/",
    ])
    assert result.exit_code == 1
    assert "private" in result.stdout.lower()


def test_add_healthcheck_rejects_loopback(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "http://127.0.0.1:8080/",
    ])
    assert result.exit_code == 1
    assert "private" in result.stdout.lower()


def test_add_healthcheck_rejects_bad_cadence(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/",
        "--every", "soon",
    ])
    assert result.exit_code == 1


def test_add_healthcheck_requires_recipient_with_deliver(
    runner,
) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/",
        "--deliver", "email",
    ])
    assert result.exit_code == 1
    assert "--to" in result.stdout


def test_add_healthcheck_rejects_zero_timeout(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/",
        "--timeout", "0",
    ])
    assert result.exit_code == 1


def test_add_healthcheck_url_quoting_neutralizes_shell_chars(
    runner,
) -> None:
    """A URL with single-quotes in it must not break out of the
    bash single-quoted variable. Defense in depth — the SSRF
    gate would already reject most exotic URLs, but we want to
    survive the founder pasting something quirky."""
    cli_runner, tmp = runner
    from korpha.cli import app
    weird_url = "https://8.8.8.8/'$(echo+pwned)'"
    result = cli_runner.invoke(app, [
        "cron", "add-healthcheck", weird_url,
        "--name", "weird-url",
    ])
    # SSRF gate may or may not accept it — what we care about is
    # that IF the script gets generated, the quoting is safe.
    if result.exit_code == 0:
        body = Path(_read_jobs(tmp)[0].script_path).read_text()
        # The single quote is escaped — no shell injection
        assert "$(echo+pwned)" in body
        # Original quote sequence is escaped via the '\\'' pattern
        assert "'\\''" in body


def test_add_healthcheck_rejects_duplicate_name(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    first = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.8.8/",
        "--name", "dup",
    ])
    assert first.exit_code == 0
    second = cli_runner.invoke(app, [
        "cron", "add-healthcheck", "https://8.8.4.4/",
        "--name", "dup",
    ])
    assert second.exit_code == 1
    assert "already exists" in second.stdout
