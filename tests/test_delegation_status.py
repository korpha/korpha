"""Tests for the delegation-status check.

We monkeypatch ``shutil.which`` and ``Path.exists`` because we don't
want the test to pass/fail based on whether the dev box happens to
have ``claude`` or ``codex`` installed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from korpha.cli import app
from korpha.delegation.status import (
    check_all,
    check_claude_code,
    check_codex_cli,
)


def _patch_path_exists(monkeypatch: pytest.MonkeyPatch, exists_paths: set[str]) -> None:
    """Make Path.exists() return True only for the given absolute paths."""
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        # Use real check for any path not in our controlled set.
        s = str(self)
        if s in exists_paths:
            return True
        return real_exists(self) if False else False

    monkeypatch.setattr(Path, "exists", fake_exists)


def test_claude_code_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    s = check_claude_code()
    assert s.installed is False
    assert s.authenticated is False
    assert s.name == "Claude Code"
    assert "install" in s.install_hint.lower()


def test_claude_code_installed_not_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    _patch_path_exists(monkeypatch, set())  # no auth files
    s = check_claude_code()
    assert s.installed is True
    assert s.authenticated is False


def test_claude_code_fully_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    _patch_path_exists(
        monkeypatch,
        {str(Path.home() / ".claude" / "credentials.json")},
    )
    s = check_claude_code()
    assert s.installed is True
    assert s.authenticated is True


def test_codex_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    s = check_codex_cli()
    assert s.installed is False
    assert s.authenticated is False
    assert "npm" in s.install_hint  # Codex installs via npm


def test_codex_installed_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    _patch_path_exists(
        monkeypatch,
        {str(Path.home() / ".codex" / "auth.json")},
    )
    s = check_codex_cli()
    assert s.installed is True
    assert s.authenticated is True


def test_check_all_returns_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    results = check_all()
    assert len(results) == 2
    names = {r.name for r in results}
    assert names == {"Claude Code", "Codex CLI"}


def test_doctor_command_prints_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """`korpha doctor` runs end-to-end without crashing and shows
    each delegation CLI's state."""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", "/nonexistent")

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Korpha health check" in result.stdout
    assert "Claude Code" in result.stdout
    assert "Codex CLI" in result.stdout
    # Provider not set + delegation not installed → all "not configured"
    assert "not configured" in result.stdout.lower() or "not installed" in result.stdout.lower()


def test_doctor_when_provider_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When provider IS configured, doctor reports it green."""
    cfg = tmp_path / "providers.yaml"
    cfg.write_text(
        "providers:\n"
        "  - preset: openai\n"
        "    api_key: sk-test\n"
        "    tiers:\n"
        "      pro: gpt-4o\n"
    )
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(cfg))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # Doctor groups output now: 'Required' header + a green check
    # under 'Inference provider' when configured. The yaml-loaded
    # provider path renders "Inference provider (via providers.yaml)".
    assert "Inference provider" in result.stdout
    assert "providers.yaml" in result.stdout
    assert "not configured" not in result.stdout.split("Inference provider")[1].split("\n")[0]
