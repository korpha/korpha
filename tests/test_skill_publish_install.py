"""Tests for skill publish (pack_skill) + LocalSource install."""
from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from korpha.skills_hub.local import (
    LocalSource, PublishResult, pack_skill,
)


# ---- pack_skill ----


@pytest.fixture
def sample_skill(tmp_path: Path) -> Path:
    """Build a tiny but realistic skill dir on disk."""
    skill = tmp_path / "my.publish_test"
    skill.mkdir()
    (skill / "manifest.yaml").write_text(
        "name: my.publish_test\n"
        "description: a test skill\n"
        "trust_level: community\n",
    )
    (skill / "skill.py").write_text(
        "def run(args): return {'ok': True}\n",
    )
    return skill


def test_pack_creates_tarball(
    sample_skill: Path, tmp_path: Path,
) -> None:
    result = pack_skill(
        sample_skill, output=tmp_path / "out.tar.gz",
    )
    assert isinstance(result, PublishResult)
    assert result.output_path.is_file()
    assert result.size_bytes > 0
    assert result.file_count >= 2  # manifest + skill.py
    assert result.skill_name == "my.publish_test"


def test_pack_default_output_uses_cwd(
    sample_skill: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = pack_skill(sample_skill)
    assert result.output_path.parent == tmp_path
    assert result.output_path.name == "my.publish_test.tar.gz"


def test_pack_excludes_junk(
    sample_skill: Path, tmp_path: Path,
) -> None:
    """__pycache__ / .git etc. must not land in the tarball."""
    (sample_skill / "__pycache__").mkdir()
    (sample_skill / "__pycache__" / "x.pyc").write_text("bytecode")
    (sample_skill / ".git").mkdir()
    (sample_skill / ".git" / "config").write_text("[git]")
    (sample_skill / ".DS_Store").write_text("hidden")

    result = pack_skill(
        sample_skill, output=tmp_path / "x.tar.gz",
    )
    with tarfile.open(result.output_path, "r:gz") as tar:
        names = tar.getnames()
    joined = " ".join(names)
    assert "__pycache__" not in joined
    assert ".git" not in joined
    assert ".DS_Store" not in joined


def test_pack_top_level_dir_matches_source_name(
    sample_skill: Path, tmp_path: Path,
) -> None:
    result = pack_skill(
        sample_skill, output=tmp_path / "x.tar.gz",
    )
    with tarfile.open(result.output_path, "r:gz") as tar:
        names = tar.getnames()
    assert all(n.startswith("my.publish_test") for n in names)


def test_pack_nonexistent_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pack_skill(tmp_path / "ghost")


def test_pack_missing_manifest_rejected(tmp_path: Path) -> None:
    skill = tmp_path / "no.manifest"
    skill.mkdir()
    (skill / "skill.py").write_text("x = 1")
    with pytest.raises(ValueError, match="manifest.yaml"):
        pack_skill(skill)


def test_pack_empty_after_excludes_raises(
    tmp_path: Path,
) -> None:
    """A directory whose contents are entirely excluded should
    error rather than produce a useless empty tarball."""
    skill = tmp_path / "junk.only"
    skill.mkdir()
    (skill / "manifest.yaml").write_text("name: junk.only")
    (skill / "__pycache__").mkdir()
    (skill / "__pycache__" / "x.pyc").write_text("y")
    # Pack with broad excludes that cover everything
    with pytest.raises(ValueError, match="no publishable"):
        pack_skill(
            skill,
            output=tmp_path / "x.tar.gz",
            excludes=("manifest.yaml", "__pycache__"),
        )


# ---- LocalSource ----


def test_local_source_fetches_directory(
    sample_skill: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    bundle = LocalSource().fetch(str(sample_skill))
    assert bundle.name == "my.publish_test"
    assert bundle.source == "local"
    assert bundle.metadata["kind"] == "directory"
    assert bundle.quarantine_path.is_dir()
    assert (bundle.quarantine_path / "manifest.yaml").is_file()


def test_local_source_fetches_tarball(
    sample_skill: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    bundle_path = tmp_path / "bundle.tar.gz"
    pack_skill(sample_skill, output=bundle_path)

    bundle = LocalSource().fetch(str(bundle_path))
    assert bundle.name == "my.publish_test"
    assert bundle.metadata["kind"] == "tarball"
    assert bundle.quarantine_path.is_dir()
    assert (bundle.quarantine_path / "manifest.yaml").is_file()


def test_local_source_search_returns_empty(
    sample_skill: Path,
) -> None:
    """Local source isn't browseable — search returns []."""
    assert LocalSource().search("anything") == []


def test_local_source_missing_path_raises(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        LocalSource().fetch(str(tmp_path / "ghost"))


def test_local_source_unknown_extension_raises(
    tmp_path: Path,
) -> None:
    file = tmp_path / "x.zip"
    file.write_text("not a tarball")
    with pytest.raises(ValueError, match="tar.gz"):
        LocalSource().fetch(str(file))


def test_local_source_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tarball with '../escape' members must be refused."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 0
        from io import BytesIO
        tar.addfile(info, BytesIO(b""))
    with pytest.raises(ValueError, match="unsafe"):
        LocalSource().fetch(str(bad))


def test_local_source_rejects_multiple_top_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    multi = tmp_path / "multi.tar.gz"
    with tarfile.open(multi, "w:gz") as tar:
        for top in ("a", "b"):
            info = tarfile.TarInfo(name=f"{top}/manifest.yaml")
            info.size = 0
            from io import BytesIO
            tar.addfile(info, BytesIO(b""))
    with pytest.raises(ValueError, match="exactly one"):
        LocalSource().fetch(str(multi))


def test_pack_then_local_install_roundtrip(
    sample_skill: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: pack a skill, install via LocalSource, get
    back a usable bundle."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    bundle_path = tmp_path / "rt.tar.gz"
    pack_skill(sample_skill, output=bundle_path)

    bundle = LocalSource().fetch(str(bundle_path))
    assert (bundle.quarantine_path / "manifest.yaml").read_text()


# ---- CLI ----


@pytest.fixture
def runner_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, Path]:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return CliRunner(), tmp_path


def test_cli_publish_creates_tarball(
    runner_env: tuple[CliRunner, Path],
    sample_skill: Path,
) -> None:
    cli_runner, tmp = runner_env
    output = tmp / "shared.tar.gz"
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "skill", "publish", str(sample_skill),
        "--output", str(output),
    ])
    assert result.exit_code == 0, result.stdout
    assert "Packed" in result.stdout
    assert output.is_file()


def test_cli_publish_no_manifest_fails_clean(
    runner_env: tuple[CliRunner, Path], tmp_path: Path,
) -> None:
    cli_runner, tmp = runner_env
    bad = tmp_path / "bad.skill"
    bad.mkdir()
    (bad / "skill.py").write_text("x = 1")  # no manifest
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "skill", "publish", str(bad),
    ])
    assert result.exit_code == 1
    assert "manifest.yaml" in result.stdout


def test_cli_install_dispatches_to_local_for_directory(
    runner_env: tuple[CliRunner, Path],
    sample_skill: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass a directory path → LocalSource path runs (no network)."""
    cli_runner, tmp = runner_env
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "skill", "install", str(sample_skill),
    ])
    # The fetch + scan + install path runs; the result depends on
    # the scanner verdict for our toy skill (community trust → likely
    # safe-or-caution). Either 0 (installed) or 2 (needs --force) is
    # acceptable for this dispatch test; what matters is we didn't
    # try the network.
    assert result.exit_code in (0, 1, 2)
    assert "fetching from local" in result.stdout


def test_cli_install_recognizes_tarball(
    runner_env: tuple[CliRunner, Path],
    sample_skill: Path, tmp_path: Path,
) -> None:
    cli_runner, tmp = runner_env
    bundle = tmp / "x.tar.gz"
    pack_skill(sample_skill, output=bundle)
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "skill", "install", str(bundle),
    ])
    # Same as above — what we assert is the local dispatch fired,
    # not the install verdict (that depends on the scanner).
    assert "fetching from local" in result.stdout


def test_cli_install_dispatches_github_for_url(
    runner_env: tuple[CliRunner, Path],
) -> None:
    """Recognizing a GitHub URL goes through GitHubSource — we
    don't mock the actual network call so we expect a fetch
    failure, but the dispatch label confirms routing."""
    cli_runner, _ = runner_env
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "skill", "install",
        "github.com/example/does-not-exist-real-test",
    ])
    assert "fetching from github" in result.stdout
    # Network call failed (or succeeded then scanner rejected) —
    # either way exit > 0
    assert result.exit_code != 0


def test_cli_install_unknown_target_tries_hub(
    runner_env: tuple[CliRunner, Path],
) -> None:
    cli_runner, _ = runner_env
    from korpha.cli import app
    result = cli_runner.invoke(app, [
        "skill", "install", "definitely-not-a-real-skill-xyz",
    ])
    assert "fetching from hub" in result.stdout
