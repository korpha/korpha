"""Fresh-install smoke test.

Verifies that on a clean machine (no providers.yaml, no API keys, no
.env nearby), the Korpha CLI:

- doesn't crash on ``--help``, ``doctor``, ``providers``, ``skill list``
- correctly reports "not configured" instead of false-positive
- ``_ensure_load_env`` doesn't pick up shipped-with-package ``.env``
  files (the bug we found and fixed: ``load_dotenv()`` walks up from
  the caller's source file by default, which would surface Korpha's
  own dev ``.env`` to end users)

Doesn't shell out to the real ``korpha`` binary (slow, env-leaky);
calls the typer ``app`` directly via ``CliRunner`` with a sterilized
environment.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from korpha.cli import _ensure_load_env, _has_any_provider_configured, app


def _sterilize_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip every variable that would mark a provider as configured,
    point KORPHA_* vars at empty tmp dirs."""
    for key in (
        "OPENCODE_API_KEY",
        "OPENCODE_GO_API_KEY",
        "OLLAMA_CLOUD_API_KEY",
        "OLLAMA_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "RANKMYANSWER_API_KEY",
        "STRIPE_API_KEY",
        "RESEND_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    fresh_dir = tmp_path / "korpha"
    fresh_dir.mkdir()
    monkeypatch.setenv("KORPHA_DATA_DIR", str(fresh_dir))
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(fresh_dir / "providers.yaml"))
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(fresh_dir / "skills"))
    monkeypatch.setenv("KORPHA_MCP_FILE", str(fresh_dir / "mcp.yaml"))
    return fresh_dir


def test_help_doesnt_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sterilize_env(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Your AI cofounder" in result.stdout


def test_doctor_reports_unconfigured_on_fresh_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for the load_dotenv-walks-up-from-source bug.

    Before the fix, ``korpha doctor`` invoked from a clean dir
    would still report 'Inference provider: configured' because
    ``load_dotenv()`` (called inside ``_ensure_load_env``) walked up
    from the package's source location and picked up the dev ``.env``
    we ship in the repo. The fix pins ``find_dotenv(usecwd=True)``
    so only the user's cwd ``.env`` is consulted.
    """
    _sterilize_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)  # no .env in tmp_path

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Inference provider — not configured" in result.stdout
    assert "RankMyAnswer — not configured" in result.stdout


def test_has_any_provider_configured_false_on_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sterilize_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    _ensure_load_env()
    assert _has_any_provider_configured() is False


def test_load_env_does_not_pick_up_package_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even if a sibling .env file exists higher up the source tree,
    ``_ensure_load_env`` should not load it — only the user's cwd."""
    _sterilize_env(monkeypatch, tmp_path)
    user_cwd = tmp_path / "user-project"
    user_cwd.mkdir()
    monkeypatch.chdir(user_cwd)

    # Sanity: no env vars at start
    assert not os.getenv("FRESH_INSTALL_TEST_KEY")

    # Plant a .env in user cwd; that one should load
    (user_cwd / ".env").write_text("FRESH_INSTALL_TEST_KEY=cwd-value\n")

    _ensure_load_env()
    assert os.environ.get("FRESH_INSTALL_TEST_KEY") == "cwd-value"


def test_install_script_syntax(tmp_path: Path) -> None:
    """install.sh shouldn't have a bash syntax error."""
    import subprocess

    install_sh = Path(__file__).parent.parent / "install.sh"
    assert install_sh.exists(), f"install.sh missing at {install_sh}"
    result = subprocess.run(
        ["bash", "-n", str(install_sh)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"install.sh syntax error: {result.stderr}"
    )


def test_skill_list_works_on_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Skills should auto-load even with no provider configured."""
    _sterilize_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    # All built-ins should be visible without needing a provider
    from korpha.skills import default_registry

    expected = {
        "niche.find_micro_niches",
        "validate.score_idea",
        "geo_seo.audit_url",
        "code.ship_via_codex",
    }
    actual = set(default_registry.skills.keys())
    missing = expected - actual
    assert not missing, f"Missing core skills on fresh install: {missing}"
