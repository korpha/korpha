"""memory namespace_id + CrossNamespaceRecallGrant

Revision ID: 8e2a5f9c1064
Revises: 2f8b6e4a3d51
Create Date: 2026-05-12

PR9 — adds namespace_id to long_term_memory_entry + the grant table.
Backfills existing rows to the business's default unit namespace so
recall queries continue to work post-migration.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '8e2a5f9c1064'
down_revision: Union[str, Sequence[str], None] = '2f8b6e4a3d51'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add namespace_id to long_term_memory_entry if the table exists.
    # (It's created via create_all in older installs.)
    inspector = sa.inspect(op.get_bind())
    if "long_term_memory_entry" in inspector.get_table_names():
        existing_cols = {
            c["name"] for c in inspector.get_columns("long_term_memory_entry")
        }
        if "namespace_id" not in existing_cols:
            with op.batch_alter_table(
                "long_term_memory_entry", schema=None,
            ) as bop:
                bop.add_column(sa.Column(
                    "namespace_id", sa.Uuid(), nullable=True,
                ))
                bop.create_index(
                    "ix_long_term_memory_entry_namespace_id",
                    ["namespace_id"], unique=False,
                )
        backfill_memory_namespaces(op.get_bind())

    op.create_table(
        "cross_namespace_recall_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("from_namespace_id", sa.Uuid(), nullable=False),
        sa.Column("to_namespace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "cooperation_proposal_id", sa.Uuid(), nullable=False,
        ),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "granted_by_agent_role_id", sa.Uuid(), nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["cooperation_proposal_id"],
            ["cooperation_proposal.id"],
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_agent_role_id"], ["agent_role.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "cross_namespace_recall_grant", schema=None,
    ) as bop:
        bop.create_index(
            "ix_cross_namespace_recall_grant_from_namespace_id",
            ["from_namespace_id"], unique=False,
        )
        bop.create_index(
            "ix_cross_namespace_recall_grant_to_namespace_id",
            ["to_namespace_id"], unique=False,
        )
        bop.create_index(
            "ix_cross_namespace_recall_grant_cooperation_proposal_id",
            ["cooperation_proposal_id"], unique=False,
        )
        bop.create_index(
            "ix_cross_namespace_recall_grant_is_active",
            ["is_active"], unique=False,
        )


def backfill_memory_namespaces(conn) -> int:
    """Point existing memory rows at their business's default unit
    namespace. Idempotent — only fills NULL.
    """
    result = conn.execute(
        sa.text(
            "UPDATE long_term_memory_entry "
            "SET namespace_id = ("
            "  SELECT u.memory_namespace_id FROM business_unit u "
            "  WHERE u.business_id = long_term_memory_entry.business_id "
            "    AND u.parent_id IS NULL "
            "    AND u.kind = 'DEFAULT' "
            "  LIMIT 1"
            ") "
            "WHERE long_term_memory_entry.namespace_id IS NULL"
        )
    )
    return result.rowcount or 0


def downgrade() -> None:
    with op.batch_alter_table(
        "cross_namespace_recall_grant", schema=None,
    ) as bop:
        bop.drop_index("ix_cross_namespace_recall_grant_is_active")
        bop.drop_index(
            "ix_cross_namespace_recall_grant_cooperation_proposal_id",
        )
        bop.drop_index(
            "ix_cross_namespace_recall_grant_to_namespace_id",
        )
        bop.drop_index(
            "ix_cross_namespace_recall_grant_from_namespace_id",
        )
    op.drop_table("cross_namespace_recall_grant")

    inspector = sa.inspect(op.get_bind())
    if "long_term_memory_entry" in inspector.get_table_names():
        existing_cols = {
            c["name"] for c in inspector.get_columns("long_term_memory_entry")
        }
        if "namespace_id" in existing_cols:
            with op.batch_alter_table(
                "long_term_memory_entry", schema=None,
            ) as bop:
                bop.drop_index("ix_long_term_memory_entry_namespace_id")
                bop.drop_column("namespace_id")
