"""Tests for the machine-migration tooling (`korpha migrate`).

Coverage:
  - Cred audit catalogue + scan against a fake $HOME
  - Manifest build + JSON round-trip + sqlite snapshot
  - Bundle round-trip: write → extract → manifest reload
  - Restore protections (existing data dir, missing bundle)
  - Wizard helpers (reauth_steps_from_manifest filters by is_present)
  - Plain `korpha backup` tarballs restore without manifest
"""
from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from korpha.migrate import (
    MACHINE_TIED_CREDS,
    MIGRATION_MANIFEST_FILENAME,
    Check,
    CheckLevel,
    Manifest,
    MachineTiedCred,
    build_manifest,
    create_migration_bundle,
    format_source_banner,
    has_blocking_failures,
    load_manifest,
    reauth_steps_from_manifest,
    restore_bundle,
    run_readiness_checks,
    scan_machine_tied,
)
from korpha.migrate.readiness import (
    _MIN_PYTHON,
    check_bundle_compatibility,
    check_data_dir_empty,
    check_disk_space,
    check_python_version,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def fake_data_dir(tmp_path: Path) -> Path:
    """A minimal Korpha-shaped data dir with a sqlite DB carrying
    the tables the snapshotter probes."""
    d = tmp_path / "korpha"
    d.mkdir()
    (d / "providers.yaml").write_text("providers: []\n")
    (d / "vault").mkdir()
    db = d / "korpha.db"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE cron_jobs (id TEXT)")
        con.execute("CREATE TABLE background_tasks (id TEXT)")
        con.execute(
            "CREATE TABLE businesses (id TEXT, is_active INTEGER)"
        )
        con.execute("INSERT INTO cron_jobs VALUES ('c1'), ('c2')")
        con.execute("INSERT INTO background_tasks VALUES ('b1')")
        con.execute(
            "INSERT INTO businesses VALUES ('biz-1', 1), ('biz-2', 0)"
        )
        con.commit()
    finally:
        con.close()
    return d


# ---------------------------------------------------------------------------
# Cred audit
# ---------------------------------------------------------------------------


def test_catalogue_covers_known_machine_tied_creds() -> None:
    names = {c.name for c in MACHINE_TIED_CREDS}
    assert "codex_cli_oauth" in names
    assert "claude_code_keychain" in names
    assert "xai_oauth" in names


def test_scan_sees_codex_auth_when_present(fake_home: Path) -> None:
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "auth.json").write_text("{}")
    result = scan_machine_tied(home=fake_home)
    by_name = {c.name: c for c in result}
    assert by_name["codex_cli_oauth"].is_present is True
    assert by_name["claude_code_keychain"].is_present is False


def test_scan_sees_claude_keychain_when_present(fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    result = scan_machine_tied(home=fake_home)
    by_name = {c.name: c for c in result}
    assert by_name["claude_code_keychain"].is_present is True


def test_scan_empty_home_marks_all_absent(fake_home: Path) -> None:
    result = scan_machine_tied(home=fake_home)
    for c in result:
        assert c.is_present is False


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_captures_source_machine(fake_data_dir: Path) -> None:
    m = build_manifest(fake_data_dir)
    assert m.manifest_version == 1
    assert m.source.hostname
    assert m.source.python_version.count(".") == 2
    assert m.source.data_dir == str(fake_data_dir)
    assert m.korpha_version


def test_manifest_pending_state_from_sqlite(fake_data_dir: Path) -> None:
    m = build_manifest(fake_data_dir)
    assert m.pending.cron_jobs == 2
    assert m.pending.background_tasks == 1
    assert m.pending.active_business_id == "biz-1"


def test_manifest_pending_state_missing_db_is_zero(tmp_path: Path) -> None:
    empty = tmp_path / "empty-korpha"
    empty.mkdir()
    m = build_manifest(empty)
    assert m.pending.cron_jobs == 0
    assert m.pending.background_tasks == 0
    assert m.pending.active_business_id is None


def test_manifest_json_round_trip(fake_data_dir: Path) -> None:
    m1 = build_manifest(fake_data_dir)
    raw = m1.to_json()
    parsed_back = json.loads(raw)
    assert parsed_back["manifest_version"] == 1
    m2 = Manifest.from_json(raw)
    assert m2.source.hostname == m1.source.hostname
    assert m2.pending.cron_jobs == m1.pending.cron_jobs
    assert len(m2.credentials_machine_tied) == len(m1.credentials_machine_tied)


def test_manifest_uses_passed_home_for_cred_scan(
    fake_data_dir: Path, fake_home: Path,
) -> None:
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "auth.json").write_text("{}")
    m = build_manifest(fake_data_dir, home=fake_home)
    by_name = {c.name: c for c in m.credentials_machine_tied}
    assert by_name["codex_cli_oauth"].is_present is True
    assert by_name["claude_code_keychain"].is_present is False


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def test_bundle_writes_tarball_with_manifest(
    fake_data_dir: Path, fake_home: Path, tmp_path: Path,
) -> None:
    out = tmp_path / "bundle.tar.gz"
    result = create_migration_bundle(fake_data_dir, out, home=fake_home)
    assert out.is_file()
    assert result.bytes_written > 0
    assert result.manifest.bundle_size_bytes == result.bytes_written

    with tarfile.open(out, "r:*") as tar:
        names = tar.getnames()
    assert MIGRATION_MANIFEST_FILENAME in names
    assert any(n.startswith("korpha/") or n == "korpha" for n in names)


def test_bundle_refuses_missing_data_dir(tmp_path: Path) -> None:
    out = tmp_path / "bundle.tar.gz"
    with pytest.raises(FileNotFoundError):
        create_migration_bundle(tmp_path / "nope", out)


def test_bundle_manifest_loadable_after_write(
    fake_data_dir: Path, fake_home: Path, tmp_path: Path,
) -> None:
    out = tmp_path / "bundle.tar.gz"
    create_migration_bundle(fake_data_dir, out, home=fake_home)
    loaded = load_manifest(out)
    assert loaded is not None
    assert loaded.manifest_version == 1
    assert loaded.source.data_dir == str(fake_data_dir)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def test_restore_round_trip(
    fake_data_dir: Path, fake_home: Path, tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle.tar.gz"
    create_migration_bundle(fake_data_dir, bundle_path, home=fake_home)

    target = tmp_path / "target-korpha"
    result = restore_bundle(bundle_path, target)

    assert result.data_dir == target.resolve()
    assert (target / "providers.yaml").is_file()
    assert (target / "korpha.db").is_file()
    assert result.manifest is not None
    assert result.manifest.source.data_dir == str(fake_data_dir)


def test_restore_refuses_to_clobber(
    fake_data_dir: Path, tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle.tar.gz"
    create_migration_bundle(fake_data_dir, bundle_path)

    target = tmp_path / "target-korpha"
    target.mkdir()
    (target / "existing-file").write_text("don't clobber me")

    with pytest.raises(FileExistsError):
        restore_bundle(bundle_path, target, force=False)
    assert (target / "existing-file").is_file()


def test_restore_force_overwrites_existing(
    fake_data_dir: Path, tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle.tar.gz"
    create_migration_bundle(fake_data_dir, bundle_path)

    target = tmp_path / "target-korpha"
    target.mkdir()
    (target / "existing-file").write_text("clobber me")

    result = restore_bundle(bundle_path, target, force=True)
    assert (target / "providers.yaml").is_file()
    assert not (target / "existing-file").exists()
    assert result.manifest is not None


def test_restore_missing_bundle(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        restore_bundle(tmp_path / "nope.tar.gz", tmp_path / "out")


def test_restore_plain_backup_tarball_has_no_manifest(
    fake_data_dir: Path, tmp_path: Path,
) -> None:
    """A pre-migrate plain `korpha backup` tarball (just korpha/, no
    manifest) should still restore — just without the wizard hook."""
    bundle_path = tmp_path / "plain-backup.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(fake_data_dir, arcname="korpha")

    target = tmp_path / "target-korpha"
    result = restore_bundle(bundle_path, target)
    assert result.manifest is None
    assert (target / "providers.yaml").is_file()


def test_restore_rejects_unsafe_paths(tmp_path: Path) -> None:
    """A bundle whose entries try to escape the extraction root must
    be ignored, leaving the data dir empty of the malicious entries.

    The legitimate ``korpha/`` member should still extract; only the
    escape attempts get skipped."""
    bundle_path = tmp_path / "evil.tar.gz"
    payload_dir = tmp_path / "real-korpha"
    payload_dir.mkdir()
    (payload_dir / "good.txt").write_text("ok")

    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(payload_dir, arcname="korpha")
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 3
        import io
        tar.addfile(info, io.BytesIO(b"bad"))

    target = tmp_path / "target-korpha"
    restore_bundle(bundle_path, target)
    assert (target / "good.txt").is_file()
    assert not (tmp_path / "escape.txt").exists()


# ---------------------------------------------------------------------------
# Wizard helpers
# ---------------------------------------------------------------------------


def test_reauth_steps_only_include_present_creds(
    fake_data_dir: Path, fake_home: Path,
) -> None:
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "auth.json").write_text("{}")
    m = build_manifest(fake_data_dir, home=fake_home)
    steps = reauth_steps_from_manifest(m)
    names = {s.name for s in steps}
    assert "codex_cli_oauth" in names
    assert "claude_code_keychain" not in names


def test_reauth_steps_empty_when_no_creds_present(
    fake_data_dir: Path, fake_home: Path,
) -> None:
    m = build_manifest(fake_data_dir, home=fake_home)
    steps = reauth_steps_from_manifest(m)
    assert steps == []


def test_format_source_banner_includes_source_metadata(
    fake_data_dir: Path,
) -> None:
    m = build_manifest(fake_data_dir)
    banner = format_source_banner(m)
    assert m.source.hostname in banner
    assert m.source.python_version in banner


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_python_version_check_passes_on_current_interpreter() -> None:
    c = check_python_version()
    assert c.level == CheckLevel.PASS


def test_python_version_check_fails_on_high_floor() -> None:
    c = check_python_version(required=(99, 0))
    assert c.level == CheckLevel.FAIL
    assert "99.0" in c.message


def test_min_python_matches_pyproject() -> None:
    """Drift-guard: readiness._MIN_PYTHON must track pyproject's
    ``requires-python`` floor. If someone bumps one without the
    other, this test catches it."""
    import re
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    requires = data["project"]["requires-python"]
    m = re.match(r"^>=\s*(\d+)\.(\d+)", requires)
    assert m is not None, (
        f"Cannot parse requires-python={requires!r}; this guard "
        "only handles '>=X.Y' form — update the guard if pyproject "
        "switches to a different constraint shape."
    )
    pyproject_floor = (int(m.group(1)), int(m.group(2)))
    assert _MIN_PYTHON == pyproject_floor, (
        f"readiness._MIN_PYTHON={_MIN_PYTHON} drifted from "
        f"pyproject requires-python={requires!r}. They must match."
    )


def test_disk_space_warn_on_high_minimum(tmp_path: Path) -> None:
    c = check_disk_space(tmp_path, min_free_bytes=10**18)
    assert c.level == CheckLevel.WARN


def test_disk_space_pass_on_low_minimum(tmp_path: Path) -> None:
    c = check_disk_space(tmp_path, min_free_bytes=1)
    assert c.level == CheckLevel.PASS


def test_data_dir_empty_passes_when_absent(tmp_path: Path) -> None:
    c = check_data_dir_empty(tmp_path / "nope")
    assert c.level == CheckLevel.PASS


def test_data_dir_empty_passes_when_empty(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    c = check_data_dir_empty(d)
    assert c.level == CheckLevel.PASS


def test_data_dir_empty_warns_when_populated(tmp_path: Path) -> None:
    d = tmp_path / "full"
    d.mkdir()
    (d / "thing").write_text("x")
    c = check_data_dir_empty(d)
    assert c.level == CheckLevel.WARN


def test_bundle_compatibility_pass_on_same_python(
    fake_data_dir: Path,
) -> None:
    m = build_manifest(fake_data_dir)
    c = check_bundle_compatibility(m)
    assert c.level == CheckLevel.PASS


def test_bundle_compatibility_info_on_adjacent_minor(
    fake_data_dir: Path,
) -> None:
    m = build_manifest(fake_data_dir)
    parts = m.source.python_version.split(".")
    m.source.python_version = f"{parts[0]}.{int(parts[1]) - 1}.0"
    c = check_bundle_compatibility(m)
    assert c.level == CheckLevel.INFO


def test_bundle_compatibility_warn_on_major_gap(
    fake_data_dir: Path,
) -> None:
    m = build_manifest(fake_data_dir)
    m.source.python_version = "2.7.18"
    c = check_bundle_compatibility(m)
    assert c.level == CheckLevel.WARN


def test_run_readiness_includes_bundle_check_when_provided(
    fake_data_dir: Path, tmp_path: Path,
) -> None:
    m = build_manifest(fake_data_dir)
    checks = run_readiness_checks(tmp_path / "fresh", manifest=m)
    names = [c.name for c in checks]
    assert "bundle_python" in names


def test_run_readiness_omits_bundle_check_when_absent(
    tmp_path: Path,
) -> None:
    checks = run_readiness_checks(tmp_path / "fresh")
    names = [c.name for c in checks]
    assert "bundle_python" not in names


def test_has_blocking_failures_true_with_fail() -> None:
    checks = [Check("a", CheckLevel.PASS, "ok"), Check("b", CheckLevel.FAIL, "x")]
    assert has_blocking_failures(checks) is True


def test_has_blocking_failures_false_without_fail() -> None:
    checks = [
        Check("a", CheckLevel.PASS, "ok"),
        Check("b", CheckLevel.WARN, "meh"),
        Check("c", CheckLevel.INFO, "fyi"),
    ]
    assert has_blocking_failures(checks) is False
