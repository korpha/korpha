"""Tests for the subdirectory hint tracker.

Covers:
  - Discovers AGENTS.md / CLAUDE.md / .cursorrules from a tree
  - Walks up ancestors when intermediate dirs have no hints
  - First-match-wins per directory (no duplicate content)
  - Per-dir is loaded only once per session (idempotent)
  - Per-file size cap with truncation marker
  - Path-token extraction from shell commands
  - Friendly relpath rendering
  - Wired into code.ship_via_codex prompt enrichment

We use tmp_path for the working dir in every test so nothing
touches the real filesystem.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from korpha.skills.subdir_hints import (
    DEFAULT_MAX_HINT_CHARS,
    SubdirectoryHintTracker,
    _path_tokens,
)


def _make_repo(tmp_path: Path) -> Path:
    """Build a small fake repo:

        repo/
          AGENTS.md            (root rules)
          src/
            main.py
            api/
              CLAUDE.md       (api-specific)
              routes.py
          tests/
            (no hint file)
            integration/
              .cursorrules   (test conventions)
              test_main.py
    """
    root = tmp_path / "repo"
    (root / "src" / "api").mkdir(parents=True)
    (root / "tests" / "integration").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# Root rules\nUse trio not asyncio")
    (root / "src" / "api" / "CLAUDE.md").write_text(
        "# API rules\nAll handlers async"
    )
    (root / "src" / "api" / "routes.py").write_text("# stub")
    (root / "src" / "main.py").write_text("# stub")
    (root / "tests" / "integration" / ".cursorrules").write_text(
        "# Test rules\nNo mocks"
    )
    (root / "tests" / "integration" / "test_main.py").write_text("# stub")
    return root


# ---- discovery ----


def test_finds_root_agents_md(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(
        working_dir=repo, assume_root_visited=False,
    )
    out = tracker.hints_for_paths([repo / "src" / "main.py"])
    assert out is not None
    assert "Use trio not asyncio" in out


def test_finds_subdir_claude_md_via_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    out = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    assert out is not None
    assert "All handlers async" in out


def test_finds_cursorrules_in_test_dir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    out = tracker.hints_for_paths([
        repo / "tests" / "integration" / "test_main.py",
    ])
    assert out is not None
    assert "No mocks" in out


def test_walks_up_ancestors_to_find_hints(tmp_path: Path) -> None:
    """``src/api/routes.py`` should pull both ``src/api/CLAUDE.md``
    AND root ``AGENTS.md`` since src/ has nothing of its own."""
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    tracker.reset()
    tracker._loaded_dirs.discard(repo)  # force root re-discovery
    out = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    assert out is not None
    assert "Use trio not asyncio" in out
    assert "All handlers async" in out


def test_root_rules_appear_before_subdir_rules(tmp_path: Path) -> None:
    """When multiple hint sections come back, shallower (more general)
    rules come first. Reading order = general → specific."""
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    tracker.reset()
    tracker._loaded_dirs.discard(repo)
    out = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    assert out is not None
    root_idx = out.find("Use trio not asyncio")
    api_idx = out.find("All handlers async")
    assert root_idx < api_idx


# ---- statefulness / idempotence ----


def test_visited_dir_not_re_emitted(tmp_path: Path) -> None:
    """Second hint discovery for the same path returns None — the
    LLM has already seen the content."""
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    first = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    second = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    assert first is not None
    assert second is None


def test_reset_re_emits(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    first = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    tracker.reset()
    second = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    assert first == second


def test_directory_with_no_hints_returns_none(tmp_path: Path) -> None:
    """``tests/`` has no hint file, only its ``integration/`` child does.
    Asking about a file directly inside ``tests/`` returns None."""
    repo = _make_repo(tmp_path)
    # Add a stub file in tests/ so the path resolves to tests/
    (repo / "tests" / "stub.py").write_text("")
    tracker = SubdirectoryHintTracker(working_dir=repo)
    # Mark working_dir loaded so we don't pull root AGENTS.md
    out = tracker.hints_for_paths([repo / "tests" / "stub.py"])
    assert out is None


# ---- caps + truncation ----


def test_oversize_hint_gets_truncated(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    big = "# Header\n" + ("padding line\n" * 5000)
    (repo / "AGENTS.md").write_text(big)
    tracker = SubdirectoryHintTracker(
        working_dir=repo, max_hint_chars=500,
    )
    tracker.reset()
    tracker._loaded_dirs.discard(repo)
    out = tracker.hints_for_paths([repo])
    assert out is not None
    assert "[...truncated AGENTS.md" in out
    # The full payload isn't there
    assert out.count("padding line") < 100


def test_default_max_chars_is_8k() -> None:
    assert DEFAULT_MAX_HINT_CHARS == 8_000


# ---- path-token extraction from shell commands ----


def test_path_tokens_picks_up_paths() -> None:
    out = _path_tokens("ls src/api/routes.py")
    assert "src/api/routes.py" in out


def test_path_tokens_skips_flags() -> None:
    out = _path_tokens("grep -rn pattern --include=*.py src/")
    assert "-rn" not in out
    assert "src/" in out


def test_path_tokens_skips_urls() -> None:
    out = _path_tokens(
        "curl https://example.com/page > local.html",
    )
    assert "https://example.com/page" not in out
    assert "local.html" in out


def test_path_tokens_handles_unparseable_shell() -> None:
    """Unbalanced quotes etc. shouldn't crash — shlex falls back."""
    out = _path_tokens("ls 'unclosed src/api")
    # Either way, something got returned without raising
    assert isinstance(out, list)


def test_hints_for_command_uses_extracted_tokens(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    out = tracker.hints_for_command(
        "vim src/api/routes.py",
    )
    assert out is not None
    assert "All handlers async" in out


# ---- relpath rendering ----


def test_friendly_relpath_renders_relative(tmp_path: Path) -> None:
    """Output contains a relative path, not the full absolute one,
    when the hint lives under working_dir."""
    repo = _make_repo(tmp_path)
    tracker = SubdirectoryHintTracker(working_dir=repo)
    out = tracker.hints_for_paths([repo / "src" / "api" / "routes.py"])
    assert out is not None
    assert "src/api/CLAUDE.md" in out
    # Absolute path is NOT in the output
    assert str(repo) + "/src/api/CLAUDE.md" not in out


# ---- code.ship_via_codex integration ----


@pytest.mark.asyncio
async def test_codex_skill_enriches_prompt_with_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: invoking ``code.ship_via_codex`` with a cwd that
    contains AGENTS.md should hand Codex a prompt that quotes the
    file in a "[Project context]" header before the user's task."""
    import shutil as _shutil

    from korpha.delegation import (
        DelegationRequest, DelegationResponse,
    )
    from korpha.skills import code_deploy
    from korpha.skills.types import SkillContext

    # Set up a fake repo with AGENTS.md
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text(
        "# Conventions\nUse pathlib not os.path"
    )

    captured: dict = {}

    class _StubCli:
        def __init__(self, *_a, **_k) -> None:
            pass
        async def run(self, request: DelegationRequest) -> DelegationResponse:
            captured["prompt"] = request.prompt
            captured["cwd"] = request.cwd
            return DelegationResponse(
                content="ok", raw_output="ok", cost_usd=0.0,
            )

    monkeypatch.setattr(code_deploy, "CodexCLI", _StubCli)
    monkeypatch.setattr(_shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(
        "korpha.skills.code_deploy.shutil.which",
        lambda _name: "/usr/bin/codex",
    )

    skill = code_deploy.ShipViaCodexSkill()

    # Build a minimal ctx + business with workspace_path
    class _Bus:
        workspace_path = repo
    class _Founder: pass

    ctx = SkillContext(
        business=_Bus(),
        founder=_Founder(),
        session=None,
        cost_tracker=None,
        invoking_agent_role_id=None,
    )
    result = await skill.run(
        ctx=ctx,
        args={
            "prompt": "Refactor the login handler",
            "cwd": str(repo),
        },
    )
    assert result.skill_name == "code.ship_via_codex"
    assert "Use pathlib not os.path" in captured["prompt"]
    assert "Project context" in captured["prompt"]
    # The user's task is preserved
    assert "Refactor the login handler" in captured["prompt"]
