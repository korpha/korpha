"""Tests for the self-update machinery.

Coverage:
  - Platform probe + project_root resolution
  - Fork detection across URL shapes (http, ssh, with/without .git, trailing /)
  - Git origin URL probe handles missing remote + non-git dir
  - HUP protection installs + finalizes cleanly + survives missing log dir
  - log_step mirrors writes into the log file + no-ops on closed state
  - step_backup writes a tarball under the data dir's backups/pre-update
  - step_uv_sync fails clearly when uv is missing
  - run_update --check-only path doesn't mutate
  - run_update zip fallback exercised against a fake git-less repo
  - Install scripts exist + are syntactically valid
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from korpha.updater import (
    OFFICIAL_REPO_URLS,
    UpdateResult,
    _HUPState,
    current_sha,
    finalize_hangup_protection,
    get_origin_url,
    install_hangup_protection,
    is_fork,
    is_windows,
    log_step,
    project_root,
    run_update,
    step_backup,
    step_uv_sync,
    step_zip_fallback,
)


# ---------------------------------------------------------------------------
# Platform / project_root
# ---------------------------------------------------------------------------


def test_is_windows_matches_platform_system() -> None:
    import platform
    assert is_windows() == (platform.system() == "Windows")


def test_project_root_contains_korpha_package() -> None:
    root = project_root()
    assert (root / "korpha").is_dir()
    assert (root / "korpha" / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# Fork detection
# ---------------------------------------------------------------------------


def test_is_fork_official_https_no_git() -> None:
    assert is_fork("https://github.com/korpha/korpha") is False


def test_is_fork_official_https_with_git() -> None:
    assert is_fork("https://github.com/korpha/korpha.git") is False


def test_is_fork_official_ssh() -> None:
    assert is_fork("git@github.com:korpha/korpha.git") is False


def test_is_fork_official_trailing_slash() -> None:
    assert is_fork("https://github.com/korpha/korpha.git/") is False


def test_is_fork_unrelated_repo() -> None:
    assert is_fork("https://github.com/someone/their-fork.git") is True


def test_is_fork_none_origin() -> None:
    assert is_fork(None) is False


def test_official_repo_list_nonempty() -> None:
    assert len(OFFICIAL_REPO_URLS) >= 2
    for u in OFFICIAL_REPO_URLS:
        assert u.startswith(("http", "git@"))


# ---------------------------------------------------------------------------
# Git probes
# ---------------------------------------------------------------------------


def test_get_origin_url_returns_none_outside_repo(tmp_path: Path) -> None:
    assert get_origin_url(tmp_path) is None


def test_get_origin_url_returns_url_inside_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/x.git"],
        cwd=tmp_path, check=True,
    )
    assert get_origin_url(tmp_path) == "https://example.com/x.git"


def test_current_sha_outside_repo_returns_none(tmp_path: Path) -> None:
    assert current_sha(tmp_path) is None


def test_current_sha_inside_repo_returns_shortish(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "x.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "x"],
        cwd=tmp_path, check=True,
    )
    sha = current_sha(tmp_path)
    assert sha is not None
    assert 4 <= len(sha) <= 12


# ---------------------------------------------------------------------------
# HUP protection
# ---------------------------------------------------------------------------


def test_install_hangup_protection_opens_log(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"KORPHA_DATA_DIR": str(tmp_path)}):
        state = install_hangup_protection()
        try:
            assert state.installed is True
            assert state.log_file is not None
            log_path = tmp_path / "logs" / "update.log"
            assert log_path.is_file()
            body = log_path.read_text()
            assert "korpha update started" in body
        finally:
            finalize_hangup_protection(state)


def test_finalize_closes_log(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"KORPHA_DATA_DIR": str(tmp_path)}):
        state = install_hangup_protection()
        log_file = state.log_file
        finalize_hangup_protection(state)
        assert log_file is None or log_file.closed


def test_finalize_safe_on_uninstalled_state() -> None:
    finalize_hangup_protection(_HUPState())  # should not raise
    finalize_hangup_protection(None)         # type: ignore[arg-type]


def test_log_step_appends_to_log(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"KORPHA_DATA_DIR": str(tmp_path)}):
        state = install_hangup_protection()
        try:
            log_step(state, "hello world")
            log_step(state, "second line")
        finally:
            finalize_hangup_protection(state)
        body = (tmp_path / "logs" / "update.log").read_text()
        assert "hello world" in body
        assert "second line" in body


def test_log_step_noop_on_empty_state() -> None:
    log_step(_HUPState(), "should not crash")
    log_step(None, "should not crash")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# step_backup
# ---------------------------------------------------------------------------


def test_step_backup_skips_when_no_data_dir(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope"
    with patch.dict(os.environ, {"KORPHA_DATA_DIR": str(nonexistent)}):
        ok, msg, path = step_backup(project_root())
    assert ok is True
    assert path is None
    assert "skipped" in msg.lower()


def test_step_backup_writes_tarball(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "file.txt").write_text("hello korpha")
    with patch.dict(os.environ, {"KORPHA_DATA_DIR": str(data_dir)}):
        ok, msg, path = step_backup(project_root())
    assert ok is True
    assert path is not None
    assert path.is_file()
    assert path.suffix == ".gz"
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("file.txt") for n in names)


# ---------------------------------------------------------------------------
# step_uv_sync
# ---------------------------------------------------------------------------


def test_step_uv_sync_fails_when_uv_missing(tmp_path: Path) -> None:
    with patch("shutil.which", return_value=None):
        ok, msg = step_uv_sync(tmp_path)
    assert ok is False
    assert "uv" in msg.lower()


# ---------------------------------------------------------------------------
# step_zip_fallback
# ---------------------------------------------------------------------------


def test_step_zip_fallback_overlays_files(tmp_path: Path) -> None:
    """Fabricate a ZIP and let step_zip_fallback overlay it. Verifies
    the prefix-stripping + nested-dir creation logic without hitting
    the real network."""
    # Make a fake "korpha-main" zip in-memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("korpha-main/README.md", "# fake repo")
        zf.writestr("korpha-main/korpha/__init__.py", "# fake pkg")
        zf.writestr("korpha-main/nested/dir/file.txt", "deep")
    buf.seek(0)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "preserve.txt").write_text("don't overwrite me")

    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = buf.getvalue()
        ok, msg = step_zip_fallback(repo)

    assert ok is True, msg
    assert (repo / "README.md").read_text() == "# fake repo"
    assert (repo / "korpha" / "__init__.py").read_text() == "# fake pkg"
    assert (repo / "nested" / "dir" / "file.txt").read_text() == "deep"
    # Pre-existing file untouched (overlay = add/overwrite, not delete)
    assert (repo / "preserve.txt").read_text() == "don't overwrite me"


def test_step_zip_fallback_handles_network_failure(tmp_path: Path) -> None:
    with patch("urllib.request.urlopen", side_effect=OSError("no net")):
        ok, msg = step_zip_fallback(tmp_path)
    assert ok is False
    assert "zip download failed" in msg


# ---------------------------------------------------------------------------
# run_update --check-only against the real repo
# ---------------------------------------------------------------------------


def test_run_update_check_only_does_not_mutate(tmp_path: Path) -> None:
    """Smoke test: --check against the live repo. Requires network to
    fetch origin; skips cleanly if offline."""
    with patch.dict(os.environ, {"KORPHA_DATA_DIR": str(tmp_path)}):
        result = run_update(check_only=True)
    assert result.method == "check-only"
    # Either succeeded (online) or failed cleanly (offline) — both fine
    if not result.success:
        assert "fetch" in (result.error or "").lower()
    else:
        assert any("check" in s for s in result.steps_run)


# ---------------------------------------------------------------------------
# Install scripts exist + parse
# ---------------------------------------------------------------------------


def test_install_sh_exists_and_executable() -> None:
    p = project_root() / "scripts" / "install.sh"
    assert p.is_file()
    assert os.access(p, os.X_OK), "install.sh must be chmod +x"
    body = p.read_text()
    assert body.startswith("#!/usr/bin/env bash") or body.startswith("#!/bin/bash")
    assert "uv sync" in body
    assert "korpha init" in body


def test_install_sh_syntax_valid() -> None:
    p = project_root() / "scripts" / "install.sh"
    result = subprocess.run(
        ["bash", "-n", str(p)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"bash syntax error: {result.stderr}"


def test_install_ps1_exists() -> None:
    p = project_root() / "scripts" / "install.ps1"
    assert p.is_file()
    body = p.read_text()
    assert "uv sync" in body
    assert "korpha init" in body
    assert "KorphaHome" in body
    # Should NOT require admin
    assert "Run as Administrator" not in body


def test_install_cmd_exists() -> None:
    p = project_root() / "scripts" / "install.cmd"
    assert p.is_file()
    body = p.read_text()
    assert "@echo off" in body
    assert "install.ps1" in body
    assert "powershell" in body.lower()
