"""Per-unit filesystem layout + backup/restore helpers.

Mirrors Paperclip's per-company directory pattern. Each BusinessUnit
gets its own subtree:

    ~/.korpha/instances/<x>/business-units/<unit-id>/
        ├── agents/
        ├── prompt-cache/
        ├── work-artifacts/
        ├── memory-blobs/
        └── backups/

Plus a shared/ dir at instance level for company-wide infrastructure
(model mesh cache, OAuth CLI configs, plugin state).

``korpha unit backup <id>`` tarballs the unit's subtree + exports
the DB rows scoped to (business_unit_id, namespace_id) into a single
archive. Restore swaps the subtree atomically + reinserts DB rows.

PR10 ships the layout helpers + backup file format. The CLI binding
lives in ``korpha.cli`` for the operator-facing command.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from korpha.business_units.model import BusinessUnit, Product


_UNIT_SUBDIRS = (
    "agents",
    "prompt-cache",
    "work-artifacts",
    "memory-blobs",
    "backups",
)
_SHARED_SUBDIRS = (
    "model-mesh-cache",
    "plugin-state",
    "oauth-cli",
    "skill-hub-catalog",
)


def instance_dir() -> Path:
    """Where ~/.korpha lives. KORPHA_DATA_DIR overrides for tests."""
    override = os.environ.get("KORPHA_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".korpha" / "instances" / "default"


def shared_dir() -> Path:
    return instance_dir() / "shared"


def unit_dir(unit_id: UUID) -> Path:
    return instance_dir() / "business-units" / str(unit_id)


def ensure_unit_layout(unit_id: UUID) -> Path:
    """Create the per-unit subtree on disk. Idempotent.

    Called when a unit is first created (HR skill spawn flow), and
    again on backup-restore. Returns the unit root path.
    """
    root = unit_dir(unit_id)
    root.mkdir(parents=True, exist_ok=True)
    for sub in _UNIT_SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    return root


def ensure_shared_layout() -> Path:
    """Create the company-wide shared/ subtree. Idempotent."""
    root = shared_dir()
    root.mkdir(parents=True, exist_ok=True)
    for sub in _SHARED_SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------


_BACKUP_MANIFEST_VERSION = 1


def backup_unit(
    session: Session,
    unit_id: UUID,
    *,
    output_path: Path | None = None,
) -> Path:
    """Tarball the unit's subtree + export its DB rows.

    Archive layout:
        manifest.json           — version, unit metadata, row counts
        filesystem/             — copy of the unit's subtree
        db/business_unit.json   — list of BusinessUnit + descendant rows
        db/business_product.json
        db/agent_memory.json    — scoped to namespace_id (PR9)
        db/kanban_card.json     — scoped to business_unit_id (PR3)
        db/cost.json
        db/cooperation_proposal.json (where unit is from_unit OR to_unit)

    Does NOT include: shared/ directory, SharedResource rows, plugin
    state. Those live at instance level and have separate backup.

    Returns the output archive path.
    """
    unit = session.get(BusinessUnit, unit_id)
    if unit is None:
        raise ValueError(f"unit {unit_id} not found")

    if output_path is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        output_path = unit_dir(unit_id) / "backups" / f"backup-{ts}.tar.gz"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as staging:
        staging_path = Path(staging)
        # 1. Manifest
        manifest = {
            "version": _BACKUP_MANIFEST_VERSION,
            "unit_id": str(unit.id),
            "unit_name": unit.name,
            "unit_slug": unit.slug,
            "unit_kind": unit.kind.value,
            "business_id": str(unit.business_id),
            "memory_namespace_id": str(unit.memory_namespace_id),
            "backed_up_at": datetime.now(UTC).isoformat(),
        }
        (staging_path / "manifest.json").write_text(
            json.dumps(manifest, indent=2)
        )

        # 2. Filesystem copy
        src = unit_dir(unit_id)
        if src.exists():
            shutil.copytree(
                src, staging_path / "filesystem",
                ignore=shutil.ignore_patterns("backups"),
            )
        else:
            (staging_path / "filesystem").mkdir()

        # 3. DB exports — JSON for portability across SQLite/Postgres
        (staging_path / "db").mkdir()
        _export_unit_db(
            session, unit, staging_path / "db",
        )

        # 4. Tar it up
        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(staging_path, arcname=".")

    return output_path


def _export_unit_db(
    session: Session, unit: BusinessUnit, out_dir: Path,
) -> None:
    """Export DB rows scoped to this unit to JSON files in out_dir."""
    # BusinessUnit + descendants
    from korpha.business_units.board import BusinessUnitBoard
    board = BusinessUnitBoard(session)
    units_in_tree = board.subtree(unit.id, include_archived=True)
    _write_json(
        out_dir / "business_unit.json",
        [_row_to_dict(u) for u in units_in_tree],
    )

    # Products under any unit in the subtree
    unit_ids = [u.id for u in units_in_tree]
    products = list(session.exec(
        select(Product).where(Product.business_unit_id.in_(unit_ids))  # type: ignore[attr-defined]
    ).all())
    _write_json(
        out_dir / "business_product.json",
        [_row_to_dict(p) for p in products],
    )

    # Optional tables — only export if they exist + have rows
    _export_if_exists(
        session, out_dir, "kanban_card",
        "business_unit_id", unit_ids,
    )
    _export_if_exists(
        session, out_dir, "agent_goal",
        "business_unit_id", unit_ids,
    )
    _export_if_exists(
        session, out_dir, "approval",
        "business_unit_id", unit_ids,
    )
    _export_if_exists(
        session, out_dir, "activity",
        "business_unit_id", unit_ids,
    )
    _export_if_exists(
        session, out_dir, "cost",
        "business_unit_id", unit_ids,
    )
    _export_if_exists(
        session, out_dir, "agent_role",
        "business_unit_id", unit_ids,
    )

    # Memory entries scoped to this unit's namespace
    namespace_ids = [u.memory_namespace_id for u in units_in_tree]
    _export_if_exists(
        session, out_dir, "long_term_memory_entry",
        "namespace_id", namespace_ids,
    )

    # Cooperation involving any of the units
    _export_cooperation(session, out_dir, unit_ids)


def _export_if_exists(
    session, out_dir: Path, table: str, key: str, values: list,
) -> None:
    """Export rows from `table` where `key IN values`. Skips if table
    doesn't exist (test environment without create_all'd tables)."""
    import sqlalchemy as sa
    inspector = sa.inspect(session.get_bind())
    if table not in inspector.get_table_names():
        return
    if not values:
        _write_json(out_dir / f"{table}.json", [])
        return
    placeholders = ", ".join(f":v{i}" for i in range(len(values)))
    params = {f"v{i}": str(v) for i, v in enumerate(values)}
    result = session.exec(sa.text(
        f"SELECT * FROM {table} WHERE {key} IN ({placeholders})"
    ).bindparams(**params))
    rows = [dict(row._mapping) for row in result.all()]
    _write_json(out_dir / f"{table}.json", rows)


def _export_cooperation(
    session, out_dir: Path, unit_ids: list,
) -> None:
    """Both perspectives — proposals where the unit is asker OR target."""
    import sqlalchemy as sa
    inspector = sa.inspect(session.get_bind())
    if "cooperation_proposal" not in inspector.get_table_names():
        return
    if not unit_ids:
        _write_json(out_dir / "cooperation_proposal.json", [])
        return
    placeholders = ", ".join(f":v{i}" for i in range(len(unit_ids)))
    params = {f"v{i}": str(v) for i, v in enumerate(unit_ids)}
    result = session.exec(sa.text(
        f"SELECT * FROM cooperation_proposal "
        f"WHERE from_unit_id IN ({placeholders}) "
        f"   OR to_unit_id IN ({placeholders})"
    ).bindparams(**params))
    rows = [dict(row._mapping) for row in result.all()]
    _write_json(out_dir / "cooperation_proposal.json", rows)


def _row_to_dict(row) -> dict:
    """SQLModel row → JSON-safe dict. UUID/datetime → str."""
    out = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, (UUID, datetime)):
            val = str(val)
        out[col.name] = val
    return out


def _write_json(path: Path, data) -> None:
    """Default str() fallback for UUIDs / datetimes still in rows."""
    path.write_text(json.dumps(data, indent=2, default=str))


__all__ = [
    "backup_unit",
    "ensure_shared_layout",
    "ensure_unit_layout",
    "instance_dir",
    "shared_dir",
    "unit_dir",
]
