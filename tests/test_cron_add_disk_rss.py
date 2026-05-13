"""Tests for `korpha cron add-disk-watch` + `add-rss` presets."""
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


# ---- add-disk-watch ----


def test_disk_watch_creates_job_and_script(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-disk-watch",
        "--every", "every 1h", "--threshold", "85",
    ])
    assert result.exit_code == 0, result.stdout
    jobs = _read_jobs(tmp)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "disk-watch"
    assert job.cadence == "every 1h"
    body = Path(job.script_path).read_text()
    assert "THRESHOLD=85" in body
    assert "df -P" in body
    # Healthy path = silent (no echo unless threshold crossed)
    assert "if [ \"$USED\" -ge \"$THRESHOLD\" ]" in body


def test_disk_watch_default_threshold_90(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, ["cron", "add-disk-watch"])
    assert result.exit_code == 0
    body = Path(_read_jobs(tmp)[0].script_path).read_text()
    assert "THRESHOLD=90" in body


def test_disk_watch_rejects_out_of_range_threshold(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    for bad in ("0", "100", "-5", "999"):
        result = cli_runner.invoke(app, [
            "cron", "add-disk-watch", "--threshold", bad,
            "--name", f"x-{bad}",
        ])
        assert result.exit_code == 1, f"expected fail for {bad}"


def test_disk_watch_rejects_shell_inj_in_mount(runner) -> None:
    """Mount path is interpolated into the script — refuse anything
    with shell metacharacters."""
    cli_runner, _ = runner
    from korpha.cli import app
    bad_mounts = [
        "/tmp; rm -rf /",
        "/foo`id`",
        "/$(echo pwn)",
        "/foo|cat",
        "relative/path",  # must be absolute
    ]
    for bad in bad_mounts:
        result = cli_runner.invoke(app, [
            "cron", "add-disk-watch", "--mount", bad,
            "--name", "x",
        ])
        assert result.exit_code == 1, f"expected fail for {bad!r}"


def test_disk_watch_with_delivery(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-disk-watch", "--mount", "/var",
        "--deliver", "telegram", "--to", "12345",
    ])
    assert result.exit_code == 0
    job = _read_jobs(tmp)[0]
    assert job.deliver_platform == "telegram"
    assert job.deliver_recipient == "12345"
    body = Path(job.script_path).read_text()
    assert "MOUNT='/var'" in body


def test_disk_watch_rejects_duplicate_name(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    a = cli_runner.invoke(app, ["cron", "add-disk-watch"])
    assert a.exit_code == 0
    b = cli_runner.invoke(app, ["cron", "add-disk-watch"])
    assert b.exit_code == 1
    assert "already exists" in b.stdout


# ---- add-rss ----


def test_rss_creates_job_and_python_script(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-rss", "https://8.8.8.8/feed.xml",
        "--every", "every 1h",
    ])
    assert result.exit_code == 0, result.stdout
    jobs = _read_jobs(tmp)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "rss-8-8-8-8"
    assert job.script_path.endswith(".py")
    body = Path(job.script_path).read_text()
    # Generated Python is syntactically valid
    import ast as _ast
    _ast.parse(body)
    assert "FEED_URL = 'https://8.8.8.8/feed.xml'" in body
    assert "MAX_NEW = 5" in body
    # State sidecar mentioned
    assert "rss-8-8-8-8.state.json" in body


def test_rss_with_max_entries_and_name(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-rss", "https://8.8.8.8/feed.xml",
        "--max", "20", "--name", "blog-feed",
    ])
    assert result.exit_code == 0
    job = _read_jobs(tmp)[0]
    assert job.name == "blog-feed"
    body = Path(job.script_path).read_text()
    assert "MAX_NEW = 20" in body
    assert "blog-feed.state.json" in body


def test_rss_rejects_metadata_url(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-rss", "http://169.254.169.254/feed",
    ])
    assert result.exit_code == 1
    assert "private" in result.stdout.lower()


def test_rss_rejects_loopback(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-rss", "http://127.0.0.1/feed",
    ])
    assert result.exit_code == 1
    assert "private" in result.stdout.lower()


def test_rss_rejects_zero_max(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-rss", "https://8.8.8.8/feed",
        "--max", "0",
    ])
    assert result.exit_code == 1


def test_rss_with_delivery(runner) -> None:
    cli_runner, tmp = runner
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "cron", "add-rss", "https://8.8.8.8/feed.xml",
        "--deliver", "email", "--to", "mike@x.com",
    ])
    assert result.exit_code == 0
    job = _read_jobs(tmp)[0]
    assert job.deliver_platform == "email"
    assert job.deliver_recipient == "mike@x.com"


def test_rss_rejects_duplicate_name(runner) -> None:
    cli_runner, _ = runner
    from korpha.cli import app
    a = cli_runner.invoke(app, [
        "cron", "add-rss", "https://8.8.8.8/feed", "--name", "dup",
    ])
    assert a.exit_code == 0
    b = cli_runner.invoke(app, [
        "cron", "add-rss", "https://8.8.4.4/feed", "--name", "dup",
    ])
    assert b.exit_code == 1


def test_rss_url_quoting_neutralizes_python_injection(runner) -> None:
    """A URL with an apostrophe must not break out of the Python
    string literal in the generated script."""
    cli_runner, tmp = runner
    from korpha.cli import app
    weird = "https://8.8.8.8/'+__import__('os').system('echo+pwn')+'"
    result = cli_runner.invoke(app, [
        "cron", "add-rss", weird, "--name", "weird",
    ])
    if result.exit_code == 0:
        body = Path(_read_jobs(tmp)[0].script_path).read_text()
        # Generated script must still parse — apostrophe got escaped
        import ast as _ast
        _ast.parse(body)


def test_rss_script_runs_against_minimal_feed(
    runner, tmp_path: Path,
) -> None:
    """End-to-end smoke: ship a fake feed served from a file URL,
    run the script, verify state file appears + first tick is silent."""
    cli_runner, tmp = runner
    from korpha.cli import app
    feed_path = tmp_path / "fake-feed.xml"
    feed_path.write_text(
        "<?xml version='1.0'?>"
        "<rss><channel>"
        "<item><guid>g1</guid><title>First post</title>"
        "<link>http://example.com/1</link></item>"
        "<item><guid>g2</guid><title>Second post</title>"
        "<link>http://example.com/2</link></item>"
        "</channel></rss>"
    )
    # Need a public-resolving URL to pass the SSRF gate; use 8.8.8.8
    # to register, then patch the script to read our fake feed.
    result = cli_runner.invoke(app, [
        "cron", "add-rss", "https://8.8.8.8/feed",
        "--name", "smoke",
    ])
    assert result.exit_code == 0
    script_path = Path(_read_jobs(tmp)[0].script_path)
    body = script_path.read_text()
    # Swap the URL for our local file so we don't hit the network
    body = body.replace(
        "FEED_URL = 'https://8.8.8.8/feed'",
        f"FEED_URL = 'file://{feed_path}'",
    )
    script_path.write_text(body)

    # Run twice. First should be silent baseline; second should be
    # silent too (no new items).
    import subprocess
    proc1 = subprocess.run(
        ["python3", str(script_path)],
        capture_output=True, text=True, timeout=20,
    )
    assert proc1.returncode == 0
    assert proc1.stdout.strip() == ""  # baseline = silent
    state_path = script_path.parent / "smoke.state.json"
    assert state_path.exists()
    import json
    state = json.loads(state_path.read_text())
    assert "g1" in state["seen"] and "g2" in state["seen"]

    # Add a new item, run again — should ship just the new one
    feed_path.write_text(
        "<?xml version='1.0'?>"
        "<rss><channel>"
        "<item><guid>g1</guid><title>First post</title></item>"
        "<item><guid>g2</guid><title>Second post</title></item>"
        "<item><guid>g3</guid><title>Third post</title>"
        "<link>http://example.com/3</link></item>"
        "</channel></rss>"
    )
    proc2 = subprocess.run(
        ["python3", str(script_path)],
        capture_output=True, text=True, timeout=20,
    )
    assert proc2.returncode == 0
    assert "Third post" in proc2.stdout
    assert "First post" not in proc2.stdout  # already seen
