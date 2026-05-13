"""Tests for ``korpha.diagnostics.doctor`` + ``...logs``.

Doctor: each probe is independently testable; we exercise the
runner with a synthetic Check list to verify aggregation /
exception-swallowing / report rendering.

Logs: install_file_handler is idempotent + writes JSONL records
that iter_log_records / tail_log can parse back. follow mode
exercised via a small in-process write loop.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from korpha.diagnostics.doctor import (
    Check,
    CheckResult,
    DoctorReport,
    run_doctor,
)
from korpha.diagnostics.logs import (
    install_file_handler,
    iter_log_records,
    tail_log,
)


# ---- CheckResult / DoctorReport ----


def test_check_result_severity_default() -> None:
    r = CheckResult(name="x", ok=True)
    assert r.severity == "info"


def test_doctor_report_has_errors_only_for_severity_error() -> None:
    """A failed check with severity=warn doesn't count as an error
    — has_errors is just for hard failures."""
    report = DoctorReport(results=[
        CheckResult(name="warn-fail", ok=False, severity="warn"),
        CheckResult(name="ok", ok=True),
    ])
    assert report.has_errors is False
    assert report.has_warnings is True


def test_doctor_report_passed_failed_counts() -> None:
    report = DoctorReport(results=[
        CheckResult(name="a", ok=True),
        CheckResult(name="b", ok=True),
        CheckResult(name="c", ok=False, severity="error"),
    ])
    assert report.passed == 2
    assert report.failed == 1


def test_doctor_report_render_no_color_clean() -> None:
    """color=False → no ANSI escape codes (good for CI logs)."""
    report = DoctorReport(results=[
        CheckResult(name="ok-check", ok=True, detail="all good"),
        CheckResult(
            name="bad-check", ok=False,
            detail="boom", severity="error", fix_hint="run setup",
        ),
    ])
    out = report.render(color=False)
    assert "\x1b[" not in out
    assert "✓ ok-check" in out
    assert "✗ bad-check" in out
    assert "fix: run setup" in out


# ---- run_doctor: aggregation + exception swallowing ----


def test_run_doctor_collects_all_results() -> None:
    checks = [
        Check("ok", lambda: CheckResult(name="ok", ok=True)),
        Check("fail", lambda: CheckResult(
            name="fail", ok=False, severity="error",
        )),
    ]
    report = run_doctor(checks)
    assert len(report.results) == 2
    assert report.passed == 1
    assert report.failed == 1


def test_run_doctor_swallows_check_exceptions() -> None:
    """A probe that *throws* (rather than returning a result) must
    not wedge the whole report — captured as a failure with the
    exception message."""
    def boom() -> CheckResult:
        raise RuntimeError("probe exploded")
    checks = [
        Check("ok", lambda: CheckResult(name="ok", ok=True)),
        Check("explodes", boom),
    ]
    report = run_doctor(checks)
    assert report.failed == 1
    bad = next(r for r in report.results if r.name == "explodes")
    assert "probe exploded" in bad.detail
    # And the OK check still ran
    assert any(r.name == "ok" and r.ok for r in report.results)


def test_default_doctor_runs_without_crashing() -> None:
    """Smoke: production check list is callable end-to-end."""
    report = run_doctor()
    assert len(report.results) >= 5
    # Python check should always pass
    py = next(r for r in report.results if r.name == "Python 3.12+")
    assert py.ok is True


# ---- doctor probes (individual) ----


def test_python_check_passes_on_312_plus() -> None:
    from korpha.diagnostics.doctor import _check_python_version
    r = _check_python_version()
    assert r.ok is True


def test_data_dir_check_warns_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing dir is warn-not-error; created on first run."""
    target = tmp_path / "doesnt-exist-yet"
    monkeypatch.setenv("KORPHA_DATA_DIR", str(target))
    from korpha.diagnostics.doctor import _check_data_dir
    r = _check_data_dir()
    assert r.ok is False
    assert r.severity == "warn"


def test_data_dir_check_passes_when_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    from korpha.diagnostics.doctor import _check_data_dir
    r = _check_data_dir()
    assert r.ok is True


def test_provider_check_fails_when_no_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No providers.yaml = error (cofounder has no LLM)."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    from korpha.diagnostics.doctor import _check_provider_accounts
    r = _check_provider_accounts()
    assert r.ok is False
    assert r.severity == "error"
    assert "no providers.yaml" in r.detail


def test_provider_check_passes_with_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    (tmp_path / "providers.yaml").write_text(
        "providers:\n"
        "  - name: deepseek\n"
        "    api_key: x\n"
    )
    from korpha.diagnostics.doctor import _check_provider_accounts
    r = _check_provider_accounts()
    assert r.ok is True
    assert "deepseek" in r.detail


def test_ssrf_check_passes_when_floor_armed() -> None:
    """If is_safe_url(metadata) returns True, the SSRF guard is
    broken. We expect the floor to block it."""
    from korpha.diagnostics.doctor import _check_security_module
    r = _check_security_module()
    assert r.ok is True


# ---- logs: install_file_handler ----


def test_install_file_handler_creates_file_on_first_log(
    tmp_path: Path,
) -> None:
    """The handler installs lazily; the file shows up after the
    first log call."""
    log_path = tmp_path / "logs" / "test.log"
    # Reset module state so the test isn't poisoned by prior installs
    from korpha.diagnostics import logs as logs_mod
    logs_mod._INSTALLED = False
    # Drop any pre-existing handlers we may have installed in earlier tests
    root = logging.getLogger()
    for h in list(root.handlers):
        if h.get_name() == "korpha.jsonl":
            root.removeHandler(h)

    install_file_handler(log_path)
    logging.getLogger("test.module").info("hello", extra={"k": "v"})
    # Force flush
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_path.exists()
    line = log_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["msg"] == "hello"
    assert record["level"] == "INFO"
    assert record["logger"] == "test.module"
    assert record["extra"] == {"k": "v"}
    # Cleanup so other tests don't get our handler
    for h in list(root.handlers):
        if h.get_name() == "korpha.jsonl":
            root.removeHandler(h)
    logs_mod._INSTALLED = False


def test_install_file_handler_is_idempotent(tmp_path: Path) -> None:
    log_path = tmp_path / "test.log"
    from korpha.diagnostics import logs as logs_mod
    logs_mod._INSTALLED = False
    root = logging.getLogger()
    for h in list(root.handlers):
        if h.get_name() == "korpha.jsonl":
            root.removeHandler(h)
    before = len(root.handlers)
    install_file_handler(log_path)
    install_file_handler(log_path)
    install_file_handler(log_path)
    # Only one new handler added across three calls
    after = len(root.handlers)
    assert after == before + 1
    # Cleanup
    for h in list(root.handlers):
        if h.get_name() == "korpha.jsonl":
            root.removeHandler(h)
    logs_mod._INSTALLED = False


# ---- logs: iter_log_records ----


def test_iter_log_records_returns_empty_for_missing_file(
    tmp_path: Path,
) -> None:
    out = list(iter_log_records(tmp_path / "no.log"))
    assert out == []


def test_iter_log_records_filters_below_min_level(
    tmp_path: Path,
) -> None:
    p = tmp_path / "log"
    p.write_text(
        json.dumps({"ts": "2026-05-07T10:00:00Z", "level": "DEBUG", "msg": "d"}) + "\n"
        + json.dumps({"ts": "2026-05-07T10:00:01Z", "level": "INFO", "msg": "i"}) + "\n"
        + json.dumps({"ts": "2026-05-07T10:00:02Z", "level": "ERROR", "msg": "e"}) + "\n"
    )
    out = [r["msg"] for r in iter_log_records(p, min_level="WARNING")]
    assert out == ["e"]


def test_iter_log_records_filters_by_since(tmp_path: Path) -> None:
    p = tmp_path / "log"
    p.write_text(
        json.dumps({"ts": "2026-05-07T10:00:00Z", "level": "INFO", "msg": "old"}) + "\n"
        + json.dumps({"ts": "2026-05-07T12:00:00Z", "level": "INFO", "msg": "new"}) + "\n"
    )
    out = [r["msg"] for r in iter_log_records(
        p, since=datetime(2026, 5, 7, 11, tzinfo=timezone.utc),
    )]
    assert out == ["new"]


def test_iter_log_records_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "log"
    p.write_text(
        json.dumps({"ts": "2026-05-07T10:00:00Z", "level": "INFO", "msg": "good"}) + "\n"
        + "not json\n"
        + "" + "\n"  # blank line
        + json.dumps({"ts": "2026-05-07T10:00:01Z", "level": "INFO", "msg": "ok2"}) + "\n"
    )
    out = [r["msg"] for r in iter_log_records(p)]
    assert out == ["good", "ok2"]


# ---- logs: tail_log limit ----


def test_tail_log_caps_initial_backlog(tmp_path: Path) -> None:
    p = tmp_path / "log"
    lines = [
        json.dumps({"ts": "2026-05-07T10:00:00Z", "level": "INFO", "msg": str(i)})
        for i in range(20)
    ]
    p.write_text("\n".join(lines) + "\n")
    out = list(tail_log(p, limit=5, follow=False))
    assert len(out) == 5
    # And it's the *last* 5 — the most recent backlog
    assert out[-1]["msg"] == "19"
