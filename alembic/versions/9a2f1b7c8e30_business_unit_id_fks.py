"""business_unit_id FKs on KanbanCard / Goal / Approval / Activity / AgentRole / CostLog + backfill

Revision ID: 9a2f1b7c8e30
Revises: eb2e487c5ec8
Create Date: 2026-05-12

Adds a nullable ``business_unit_id`` column on each of the 6 existing
tables that need org-tree scoping. After PR1+PR2 there's exactly one
default BusinessUnit per Business, so backfill is deterministic: every
existing row gets pointed at its Business's default unit.

Nullable for now — a future migration tightens to non-null once
production rollouts have backfilled cleanly.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9a2f1b7c8e30'
down_revision: Union[str, Sequence[str], None] = 'eb2e487c5ec8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TARGETS: tuple[tuple[str, str], ...] = (
    # (table_name, index_name)
    ("kanban_card", "ix_kanban_card_business_unit_id"),
    ("agent_goal", "ix_agent_goal_business_unit_id"),
    ("approval", "ix_approval_business_unit_id"),
    ("activity", "ix_activity_business_unit_id"),
    ("agent_role", "ix_agent_role_business_unit_id"),
    ("cost", "ix_cost_business_unit_id"),
)


def upgrade() -> None:
    """Add nullable business_unit_id columns + indexes + backfill.

    Pre-existing Korpha installs create most tables via
    ``SQLModel.metadata.create_all()`` at app startup rather than
    alembic — so on those installs, the target tables already exist
    before this migration runs. We defensively skip any target table
    that isn't present (only happens in fresh-alembic-only test
    environments) so the chain doesn't error mid-flight.
    """
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())

    for table_name, index_name in _TARGETS:
        if table_name not in existing_tables:
            # Test environment that hasn't called create_all yet —
            # the column will be created when the model is later
            # materialized.
            continue
        existing_cols = {
            col["name"] for col in inspector.get_columns(table_name)
        }
        if "business_unit_id" in existing_cols:
            # Re-run protection — column already added
            continue
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                "business_unit_id", sa.Uuid(), nullable=True,
            ))
            batch_op.create_foreign_key(
                f"fk_{table_name}_business_unit",
                "business_unit",
                ["business_unit_id"], ["id"],
            )
            batch_op.create_index(
                index_name, ["business_unit_id"], unique=False,
            )

    backfill_business_unit_ids(op.get_bind())


def backfill_business_unit_ids(conn) -> dict[str, int]:
    """Idempotent backfill — points every row's business_unit_id at the
    business's default BusinessUnit. Returns counts per table for the
    upgrade log + tests.

    Safe to re-run. Rows that already have a non-null business_unit_id
    are left alone.
    """
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    counts: dict[str, int] = {}
    for table_name, _ in _TARGETS:
        if table_name not in existing_tables:
            counts[table_name] = 0
            continue
        existing_cols = {
            col["name"] for col in inspector.get_columns(table_name)
        }
        if "business_unit_id" not in existing_cols:
            # Column not added yet (test bypass path); nothing to backfill
            counts[table_name] = 0
            continue
        result = conn.execute(
            sa.text(
                f"UPDATE {table_name} "
                f"SET business_unit_id = ("
                f"  SELECT u.id FROM business_unit u "
                f"  WHERE u.business_id = {table_name}.business_id "
                f"    AND u.parent_id IS NULL "
                f"    AND u.kind = 'DEFAULT' "
                f"  LIMIT 1"
                f") "
                f"WHERE {table_name}.business_unit_id IS NULL"
            )
        )
        counts[table_name] = result.rowcount or 0
    return counts


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for table_name, index_name in reversed(_TARGETS):
        if table_name not in existing_tables:
            continue
        existing_cols = {
            col["name"] for col in inspector.get_columns(table_name)
        }
        if "business_unit_id" not in existing_cols:
            continue
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.drop_index(index_name)
            batch_op.drop_constraint(
                f"fk_{table_name}_business_unit", type_="foreignkey",
            )
            batch_op.drop_column("business_unit_id")
