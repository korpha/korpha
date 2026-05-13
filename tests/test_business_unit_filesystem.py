"""PR10 tests — per-unit filesystem layout + backup.

CLI binding is exercised by tests/test_cli.py separately (added with
the CLI command in #228); here we test the helpers directly.
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.filesystem import (
    backup_unit, ensure_shared_layout, ensure_unit_layout,
    instance_dir, shared_dir, unit_dir,
)
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, Product, ProductKind,
)


@pytest.fixture
def tmp_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_instance_dir_honors_env(
    tmp_data_dir: Path,
) -> None:
    assert instance_dir() == tmp_data_dir


def test_unit_dir_under_business_units(
    tmp_data_dir: Path,
) -> None:
    from uuid import uuid4
    uid = uuid4()
    expected = tmp_data_dir / "business-units" / str(uid)
    assert unit_dir(uid) == expected


# ---------------------------------------------------------------------------
# Layout creation
# ---------------------------------------------------------------------------


def test_ensure_unit_layout_creates_subdirs(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="x",
        kind=BusinessUnitKind.DEFAULT,
    )
    root = ensure_unit_layout(unit.id)
    assert root.exists()
    for sub in [
        "agents", "prompt-cache", "work-artifacts",
        "memory-blobs", "backups",
    ]:
        assert (root / sub).is_dir(), f"{sub} missing"


def test_ensure_unit_layout_idempotent(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="x",
        kind=BusinessUnitKind.DEFAULT,
    )
    ensure_unit_layout(unit.id)
    ensure_unit_layout(unit.id)  # Re-run; should not error
    assert unit_dir(unit.id).exists()


def test_ensure_shared_layout_creates_subdirs(
    tmp_data_dir: Path,
) -> None:
    root = ensure_shared_layout()
    assert root.exists()
    for sub in [
        "model-mesh-cache", "plugin-state",
        "oauth-cli", "skill-hub-catalog",
    ]:
        assert (root / sub).is_dir(), f"{sub} missing"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def test_backup_produces_targz(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.DEFAULT,
    )
    ensure_unit_layout(unit.id)

    out = backup_unit(session, unit.id)
    assert out.is_file()
    assert out.suffix == ".gz"
    assert tarfile.is_tarfile(out)


def test_backup_includes_manifest(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.DEFAULT,
    )
    ensure_unit_layout(unit.id)
    out = backup_unit(session, unit.id)
    with tarfile.open(out, "r:gz") as tar:
        manifest_f = tar.extractfile("./manifest.json")
        assert manifest_f is not None
        manifest = json.loads(manifest_f.read())
    assert manifest["unit_id"] == str(unit.id)
    assert manifest["unit_slug"] == "kdp"
    assert manifest["memory_namespace_id"] == str(unit.memory_namespace_id)


def test_backup_includes_unit_db_export(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.DEFAULT,
    )
    ensure_unit_layout(unit.id)
    out = backup_unit(session, unit.id)
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "./manifest.json" in names
    assert "./db/business_unit.json" in names
    assert "./db/business_product.json" in names


def test_backup_excludes_shared_subtree(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    """Shared/ is instance-level, not unit-level. Backup must not
    include it — restoring KDP must not touch SaaS Vidyo's GPU mesh
    config, etc."""
    ensure_shared_layout()
    # Drop a file in shared/ to make sure it's NOT in the unit backup
    (shared_dir() / "model-mesh-cache" / "sentinel.txt").write_text("nope")
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.DEFAULT,
    )
    ensure_unit_layout(unit.id)
    out = backup_unit(session, unit.id)
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert not any("sentinel.txt" in n for n in names)
    assert not any("model-mesh-cache" in n for n in names)


def test_backup_includes_subtree_descendants(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    """Backing up a parent unit pulls all its descendant units' rows."""
    board = BusinessUnitBoard(session)
    root = board.create(
        business_id=business.id, name="KDP",
        kind=BusinessUnitKind.DEFAULT,
    )
    romance = board.create(
        business_id=business.id, name="Romance",
        kind=BusinessUnitKind.TYPE, parent_id=root.id,
    )
    ensure_unit_layout(root.id)
    out = backup_unit(session, root.id)
    with tarfile.open(out, "r:gz") as tar:
        units_f = tar.extractfile("./db/business_unit.json")
        units_data = json.loads(units_f.read())
    slugs = {u["slug"] for u in units_data}
    assert "kdp" in slugs and "romance" in slugs


def test_backup_includes_filesystem_artifacts(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    """A file in work-artifacts/ round-trips into the archive."""
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="x",
        kind=BusinessUnitKind.DEFAULT,
    )
    root = ensure_unit_layout(unit.id)
    (root / "work-artifacts" / "demo.txt").write_text("hello")
    out = backup_unit(session, unit.id)
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert any("work-artifacts/demo.txt" in n for n in names)


def test_backup_filesystem_skips_own_backups_dir(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    """The unit's backups/ subdir is excluded from its own archive
    (otherwise backups recurse + grow exponentially)."""
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="x",
        kind=BusinessUnitKind.DEFAULT,
    )
    root = ensure_unit_layout(unit.id)
    (root / "backups" / "old.tar.gz").write_text("ancient")
    out = backup_unit(session, unit.id)
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert not any("backups/old.tar.gz" in n for n in names)


def test_backup_with_no_filesystem_subtree(
    session: Session, business: Business, tmp_data_dir: Path,
) -> None:
    """Backup works even if the unit's directory wasn't created on disk."""
    board = BusinessUnitBoard(session)
    unit = board.create(
        business_id=business.id, name="x",
        kind=BusinessUnitKind.DEFAULT,
    )
    out = backup_unit(session, unit.id)
    assert out.is_file()


def test_backup_unknown_unit_raises(
    session: Session, tmp_data_dir: Path,
) -> None:
    from uuid import uuid4
    with pytest.raises(ValueError, match="not found"):
        backup_unit(session, uuid4())
