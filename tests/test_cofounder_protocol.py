"""Cofounder Protocol — manifest validation + install/list/uninstall."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from korpha.cli import app
from korpha.protocol import (
    AuthSpec,
    CofounderManifest,
    ManifestError,
    install_manifest,
    list_installed,
    load_manifest,
    parse_manifest,
    uninstall_manifest,
)

# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


_VALID_MANIFEST = {
    "spec_version": 1,
    "name": "rank_my_answer",
    "display_name": "RankMyAnswer.com — GEO + SEO",
    "description": "GEO + SEO audits.",
    "homepage": "https://rankmyanswer.com",
    "provides": {"skills": ["geo_seo.audit_url"]},
    "auth": {
        "kind": "api_key",
        "api_key_env": "RANKMYANSWER_API_KEY",
        "setup_command": "korpha config-rankmyanswer-add",
        "signup_url": "https://rankmyanswer.com/signup",
    },
    "branding": {"primary_color": "#1f7a4d"},
    "requires": {"network_egress": ["api.rankmyanswer.com"]},
}


def test_parse_valid_manifest() -> None:
    m = parse_manifest(_VALID_MANIFEST)
    assert m.name == "rank_my_answer"
    assert m.display_name.startswith("RankMyAnswer")
    assert m.provides.skills == ("geo_seo.audit_url",)
    assert m.auth is not None
    assert m.auth.kind == "api_key"
    assert m.auth.setup_command == "korpha config-rankmyanswer-add"
    assert m.branding is not None
    assert m.branding.primary_color == "#1f7a4d"


def test_missing_required_field_rejected() -> None:
    body = dict(_VALID_MANIFEST)
    del body["display_name"]
    with pytest.raises(ManifestError, match=r"display_name"):
        parse_manifest(body)


def test_unsupported_spec_version_rejected() -> None:
    body = dict(_VALID_MANIFEST)
    body["spec_version"] = 99
    with pytest.raises(ManifestError, match=r"unsupported spec_version"):
        parse_manifest(body)


def test_non_snake_case_name_rejected() -> None:
    body = dict(_VALID_MANIFEST)
    body["name"] = "RankMyAnswer"  # CamelCase
    with pytest.raises(ManifestError, match=r"snake_case"):
        parse_manifest(body)


def test_non_https_homepage_rejected() -> None:
    body = dict(_VALID_MANIFEST)
    body["homepage"] = "rankmyanswer.com"  # missing scheme
    with pytest.raises(ManifestError, match=r"homepage"):
        parse_manifest(body)


def test_empty_provides_rejected() -> None:
    body = dict(_VALID_MANIFEST)
    body["provides"] = {"skills": []}
    with pytest.raises(ManifestError, match=r"at least one skill"):
        parse_manifest(body)


def test_invalid_auth_kind_rejected() -> None:
    body = dict(_VALID_MANIFEST)
    body["auth"] = {"kind": "magic"}  # not api_key/oauth/none
    with pytest.raises(ManifestError, match=r"auth\.kind"):
        parse_manifest(body)


def test_bad_hex_color_rejected() -> None:
    body = dict(_VALID_MANIFEST)
    body["branding"] = {"primary_color": "green"}
    with pytest.raises(ManifestError, match=r"primary_color"):
        parse_manifest(body)


def test_load_manifest_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "cofounder.yaml"
    p.write_text(yaml.safe_dump(_VALID_MANIFEST))
    m = load_manifest(p)
    assert m.name == "rank_my_answer"
    assert m.source_path == p.resolve()


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match=r"not found"):
        load_manifest(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# Installer (offline)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_install_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    install_root = tmp_path / "cofounders"
    monkeypatch.setenv("KORPHA_COFOUNDERS_DIR", str(install_root))
    return install_root


def _write_manifest(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "cofounder.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_install_manifest_creates_partner_dir(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    src = _write_manifest(tmp_path, _VALID_MANIFEST)
    installed = install_manifest(src)
    assert installed.install_dir == isolated_install_dir / "rank_my_answer"
    assert (installed.install_dir / "cofounder.yaml").exists()
    assert installed.manifest.name == "rank_my_answer"


def test_install_manifest_rejects_unknown_skill(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    body = dict(_VALID_MANIFEST)
    body["provides"] = {"skills": ["fictional.skill"]}
    src = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestError, match=r"not in the Korpha skill registry"):
        install_manifest(src)


def test_install_manifest_skip_skill_check(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    body = dict(_VALID_MANIFEST)
    body["provides"] = {"skills": ["fictional.skill"]}
    src = _write_manifest(tmp_path, body)
    # Should not raise
    installed = install_manifest(src, skip_skill_check=True)
    assert installed.manifest.name == "rank_my_answer"


def test_list_installed_picks_up_partners(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    src = _write_manifest(tmp_path, _VALID_MANIFEST)
    install_manifest(src)
    listed = list_installed()
    assert len(listed) == 1
    assert listed[0].manifest.name == "rank_my_answer"


def test_uninstall_removes_partner_dir(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    src = _write_manifest(tmp_path, _VALID_MANIFEST)
    installed = install_manifest(src)
    assert installed.install_dir.exists()
    assert uninstall_manifest("rank_my_answer") is True
    assert not installed.install_dir.exists()
    assert uninstall_manifest("rank_my_answer") is False  # second call is a no-op


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cofounder_install_cli(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    src = _write_manifest(tmp_path, _VALID_MANIFEST)
    runner = CliRunner()
    result = runner.invoke(app, ["cofounder", "install", str(src)])
    assert result.exit_code == 0, result.stdout
    assert "Installed cofounder partner" in result.stdout
    assert "config-rankmyanswer-add" in result.stdout  # setup command surfaced


def test_cofounder_list_cli_empty(
    isolated_install_dir: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["cofounder", "list"])
    assert result.exit_code == 0
    assert "No cofounder partners installed" in result.stdout


def test_cofounder_list_cli_with_partner(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    src = _write_manifest(tmp_path, _VALID_MANIFEST)
    install_manifest(src)
    runner = CliRunner()
    result = runner.invoke(app, ["cofounder", "list"])
    assert result.exit_code == 0
    assert "rank_my_answer" in result.stdout
    assert "RankMyAnswer.com" in result.stdout


def test_cofounder_uninstall_cli(
    isolated_install_dir: Path, tmp_path: Path
) -> None:
    src = _write_manifest(tmp_path, _VALID_MANIFEST)
    install_manifest(src)
    runner = CliRunner()
    result = runner.invoke(app, ["cofounder", "uninstall", "rank_my_answer"])
    assert result.exit_code == 0
    assert "Uninstalled" in result.stdout


def test_cofounder_uninstall_cli_unknown_returns_nonzero(
    isolated_install_dir: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["cofounder", "uninstall", "ghost_partner"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Reference example loads cleanly
# ---------------------------------------------------------------------------


def test_reference_rankmyanswer_example_validates() -> None:
    """The shipped example manifest must pass spec validation —
    otherwise partners can't trust it as a copy-paste starting point."""
    example = (
        Path(__file__).parent.parent
        / "korpha"
        / "protocol"
        / "examples"
        / "rank_my_answer.cofounder.yaml"
    )
    assert example.exists(), f"missing reference manifest at {example}"
    m = load_manifest(example)
    assert m.name == "rank_my_answer"
    assert m.auth is not None and m.auth.kind == "api_key"


def test_auth_dataclass_optional_fields() -> None:
    """AuthSpec should tolerate omitted optional fields."""
    spec = AuthSpec(kind="none")
    assert spec.kind == "none"
    assert spec.api_key_env is None
    assert spec.setup_command is None


def test_manifest_immutable() -> None:
    m = parse_manifest(_VALID_MANIFEST)
    with pytest.raises(Exception):  # noqa: B017 — frozen dataclass raises FrozenInstanceError
        m.name = "evil_partner"  # type: ignore[misc]


def test_cofounder_manifest_dataclass() -> None:
    """Sanity: the dataclass shape is what we expose to docs."""
    m = parse_manifest(_VALID_MANIFEST)
    assert isinstance(m, CofounderManifest)
    assert m.spec_version == 1
