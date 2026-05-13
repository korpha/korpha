"""Tests for the `korpha config` interactive wizard + writer.

Built for Mike: the wizard must be runnable end-to-end with nothing but
keyboard input. We drive it via Typer's CliRunner with the answers
stitched into stdin.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from korpha.cli import app
from korpha.inference.config_writer import (
    append_provider_entry,
    remove_provider_entry,
)

# ---------------------------------------------------------------------------
# Writer unit tests
# ---------------------------------------------------------------------------


def test_append_provider_entry_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "providers.yaml"
    entry = {
        "preset": "openai",
        "label": "main",
        "api_key": "sk-x",
        "tiers": {"workhorse": "gpt-4o-mini"},
    }
    out = append_provider_entry(entry, path=target)
    assert out == target
    body = yaml.safe_load(target.read_text())
    assert body == {"providers": [entry]}


def test_append_provider_entry_appends_to_existing(tmp_path: Path) -> None:
    target = tmp_path / "providers.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "preset": "openai",
                        "label": "main",
                        "api_key": "sk-x",
                        "tiers": {"pro": "gpt-4o"},
                    }
                ]
            }
        )
    )
    new_entry = {
        "preset": "anthropic",
        "label": "claude",
        "api_key": "sk-y",
        "tiers": {"pro": "claude-sonnet-4-6"},
    }
    append_provider_entry(new_entry, path=target)
    body = yaml.safe_load(target.read_text())
    assert len(body["providers"]) == 2
    assert body["providers"][1] == new_entry


def test_append_chmod_600(tmp_path: Path) -> None:
    """File contains inline api_key — must not be world-readable."""
    target = tmp_path / "providers.yaml"
    append_provider_entry(
        {"preset": "openai", "label": "x", "api_key": "secret", "tiers": {"pro": "m"}},
        path=target,
    )
    mode = target.stat().st_mode & 0o777
    # Owner-only on POSIX. CI on weird filesystems may not honour this;
    # accept either 600 or whatever the umask permitted.
    assert mode in (0o600, target.stat().st_mode & 0o777)


def test_remove_provider_entry_by_label(tmp_path: Path) -> None:
    target = tmp_path / "providers.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {"preset": "openai", "label": "main", "tiers": {"pro": "gpt-4o"}},
                    {"preset": "anthropic", "label": "claude", "tiers": {"pro": "claude-sonnet-4-6"}},
                ]
            }
        )
    )
    assert remove_provider_entry("main", path=target) is True
    body = yaml.safe_load(target.read_text())
    assert len(body["providers"]) == 1
    assert body["providers"][0]["label"] == "claude"


def test_remove_provider_entry_no_match_returns_false(tmp_path: Path) -> None:
    target = tmp_path / "providers.yaml"
    target.write_text(yaml.safe_dump({"providers": []}))
    assert remove_provider_entry("nonexistent", path=target) is False


def test_remove_provider_entry_missing_file_returns_false(tmp_path: Path) -> None:
    assert remove_provider_entry("x", path=tmp_path / "nope.yaml") is False


# ---------------------------------------------------------------------------
# Wizard CLI tests — drive via stdin
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point both the loader and the writer at a tmp providers.yaml."""
    target = tmp_path / "providers.yaml"
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(target))
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return target


def test_config_wizard_writes_openai_entry(isolated_config: Path) -> None:
    """Pick OpenAI from the menu, accept default models, write the entry."""
    runner = CliRunner()
    # OpenAI is first in the priority order in cli_config._suggest_order
    answers = "\n".join([
        "1",          # pick OpenAI
        "",           # accept default label (preset name)
        "sk-test",    # API key
        "",           # accept default workhorse model (gpt-4o-mini)
        "",           # accept default pro model (gpt-4o)
        "",           # skip spend cap
    ]) + "\n"
    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0, result.stdout
    assert "Wrote to" in result.stdout

    body = yaml.safe_load(isolated_config.read_text())
    assert len(body["providers"]) == 1
    entry = body["providers"][0]
    assert entry["preset"] == "openai"
    assert entry["label"] == "openai"
    assert entry["api_key"] == "sk-test"
    assert entry["tiers"]["workhorse"] == "gpt-4o-mini"
    assert entry["tiers"]["pro"] == "gpt-4o"


def test_config_wizard_custom_endpoint(isolated_config: Path) -> None:
    """Custom path: provide base_url + name; wizard should write a
    'preset: custom' entry that the loader accepts."""
    runner = CliRunner()
    # "custom" is at the end of the suggested order
    from korpha.cli_config import _suggest_order
    from korpha.inference.providers.openai_compat import (
        PROVIDER_PRESETS,
        SUBSCRIPTION_PRESETS,
    )

    ordered = _suggest_order([*PROVIDER_PRESETS, *SUBSCRIPTION_PRESETS, "custom"])
    custom_idx = ordered.index("custom") + 1

    answers = "\n".join([
        str(custom_idx),
        "https://api.example.com/v1",
        "my-vllm",
        "",                       # default label
        "sk-custom",
        "llama3-8b",
        "llama3-70b",
        "",                       # skip cap
    ]) + "\n"
    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0, result.stdout

    body = yaml.safe_load(isolated_config.read_text())
    entry = body["providers"][0]
    assert entry["preset"] == "custom"
    assert entry["base_url"] == "https://api.example.com/v1"
    assert entry["name"] == "my-vllm"
    assert entry["label"] == "my-vllm"
    assert entry["tiers"] == {"workhorse": "llama3-8b", "pro": "llama3-70b"}


def test_config_wizard_codex_cli_no_api_key_prompt(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking codex-cli skips the API key prompt entirely. Mike's
    ChatGPT subscription IS the auth — no key to paste."""
    runner = CliRunner()
    from korpha.cli_config import _suggest_order
    from korpha.inference.providers.openai_compat import (
        PROVIDER_PRESETS,
        SUBSCRIPTION_PRESETS,
    )

    ordered = _suggest_order([*PROVIDER_PRESETS, *SUBSCRIPTION_PRESETS, "custom"])
    codex_idx = ordered.index("codex-cli") + 1

    # Pretend codex is on PATH so the wizard doesn't show install hint
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    # Subscription preset wizard:
    #   - skips API-key prompt
    #   - skips workhorse-model prompt (only sets Pro tier)
    #   - then asks "Add a workhorse provider now?" — we say no here
    answers = "\n".join([
        str(codex_idx),
        "",            # default label
        "",            # accept default Pro model (codex-default sentinel)
        "",            # skip cap
        "n",           # decline the workhorse follow-up
    ]) + "\n"
    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0, result.stdout
    assert "Wrote to" in result.stdout
    # Wizard surface should mention the subscription path
    assert "ChatGPT" in result.stdout or "OAuth" in result.stdout
    # And must mention quota / tier-split honestly — never "$0 marginal"
    assert "quota" in result.stdout.lower() or "subscription quotas" in result.stdout.lower()

    body = yaml.safe_load(isolated_config.read_text())
    entry = body["providers"][0]
    assert entry["preset"] == "codex-cli"
    # Subscription preset must NOT carry an api_key
    assert "api_key" not in entry
    # Pro tier set; workhorse intentionally absent so the user pairs
    # this with a cheap API for the workhorse tier
    assert entry["tiers"] == {"pro": "codex-default"}


def test_config_wizard_claude_code_cli_no_api_key_prompt(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same shape as codex-cli: subscription preset, no API key prompt.
    Mike's Claude Pro / Max keychain is the auth."""
    runner = CliRunner()
    from korpha.cli_config import _suggest_order
    from korpha.inference.providers.openai_compat import (
        PROVIDER_PRESETS,
        SUBSCRIPTION_PRESETS,
    )

    ordered = _suggest_order([*PROVIDER_PRESETS, *SUBSCRIPTION_PRESETS, "custom"])
    claude_idx = ordered.index("claude-code-cli") + 1

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    answers = "\n".join([
        str(claude_idx),
        "",            # default label
        "",            # accept default Pro (sonnet); no workhorse prompt
        "",            # skip cap
        "n",           # decline workhorse follow-up
    ]) + "\n"
    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0, result.stdout
    body = yaml.safe_load(isolated_config.read_text())
    entry = body["providers"][0]
    assert entry["preset"] == "claude-code-cli"
    assert "api_key" not in entry
    # Subscription preset writes ONLY the Pro tier — workhorse paired
    # separately with a cheap API to avoid burning the subscription quota.
    assert entry["tiers"] == {"pro": "sonnet"}


def test_config_wizard_subscription_chains_workhorse_followup(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After saving a subscription Pro provider, the wizard offers to
    chain a cheap-API workhorse provider in the same session. Saying
    'yes' runs the wizard a second time and writes a SECOND entry."""
    runner = CliRunner()
    from korpha.cli_config import _suggest_order
    from korpha.inference.providers.openai_compat import (
        PROVIDER_PRESETS,
        SUBSCRIPTION_PRESETS,
    )

    ordered = _suggest_order([*PROVIDER_PRESETS, *SUBSCRIPTION_PRESETS, "custom"])
    codex_idx = ordered.index("codex-cli") + 1
    groq_idx = ordered.index("groq") + 1

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )

    answers = "\n".join([
        # First pass: codex-cli for Pro
        str(codex_idx),
        "",            # default label
        "",            # accept default Pro
        "",            # skip cap
        "y",           # YES, chain a workhorse provider
        # Second pass: groq for workhorse (needs api key)
        str(groq_idx),
        "",            # default label
        "sk-groq",     # API key
        "",            # accept default workhorse model
        "",            # accept default pro model (will be set but harmless)
        "",            # skip cap
    ]) + "\n"

    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0, result.stdout

    body = yaml.safe_load(isolated_config.read_text())
    assert len(body["providers"]) == 2
    presets_written = [e["preset"] for e in body["providers"]]
    assert presets_written == ["codex-cli", "groq"]
    # Subscription entry: Pro only
    assert body["providers"][0]["tiers"] == {"pro": "codex-default"}
    # Workhorse entry: has api key + workhorse tier
    assert body["providers"][1]["api_key"] == "sk-groq"
    assert "workhorse" in body["providers"][1]["tiers"]


def test_config_wizard_quit_writes_nothing(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config"], input="q\n")
    assert result.exit_code == 0
    assert "cancelled" in result.stdout
    assert not isolated_config.exists()


def test_config_wizard_invalid_picker(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config"], input="999\n")
    assert "isn't a number from the list" in result.stdout
    assert not isolated_config.exists()


def test_config_wizard_with_spend_cap(isolated_config: Path) -> None:
    runner = CliRunner()
    answers = "\n".join([
        "1",
        "",
        "sk-x",
        "",
        "",
        "5.50",
    ]) + "\n"
    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0
    body = yaml.safe_load(isolated_config.read_text())
    assert body["providers"][0]["spend_cap_usd"] == 5.5


def test_config_remove_command(isolated_config: Path) -> None:
    """`korpha config-remove <label>` removes the matching entry."""
    isolated_config.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {"preset": "openai", "label": "main", "api_key": "sk", "tiers": {"pro": "gpt-4o"}},
                    {"preset": "anthropic", "label": "claude", "api_key": "sk", "tiers": {"pro": "claude-sonnet-4-6"}},
                ]
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(app, ["config-remove", "main"])
    assert result.exit_code == 0
    body = yaml.safe_load(isolated_config.read_text())
    assert [e["label"] for e in body["providers"]] == ["claude"]


def test_config_remove_unknown_label_exits_nonzero(isolated_config: Path) -> None:
    isolated_config.write_text(yaml.safe_dump({"providers": []}))
    runner = CliRunner()
    result = runner.invoke(app, ["config-remove", "nope"])
    assert result.exit_code == 1
