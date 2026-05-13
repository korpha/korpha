"""cooperation_proposal + cross_unit_query_log tables + STRATEGIC ActionClass

Revision ID: 2f8b6e4a3d51
Revises: 7d3f9c4e1820
Create Date: 2026-05-12

PR8 — adds CooperationProposal (cross-unit voluntary agreements) +
CrossUnitQueryLog (audit trail for ``cooperation.ask_about`` calls).

Postgres requires explicit ENUM value-add; SQLite tolerates string
storage. The Approval.action_class column is already a TEXT-backed
enum in SQLite via SQLAlchemy's StrEnum, so adding STRATEGIC works
without a schema change for SQLite. Postgres deployments need the
ALTER TYPE ... ADD VALUE in this migration.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '2f8b6e4a3d51'
down_revision: Union[str, Sequence[str], None] = '7d3f9c4e1820'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_col() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "cooperation_proposal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("from_unit_id", sa.Uuid(), nullable=False),
        sa.Column("to_unit_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("details", sa.String(), nullable=False),
        sa.Column("proposed_terms", _json_col(), nullable=False),
        sa.Column("permissions", _json_col(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PROPOSED", "ACCEPTED", "DECLINED",
                "ESCALATED_CEO", "ESCALATED_FOUNDER",
                "EXPIRED", "REVOKED",
                name="cooperationstatus",
            ),
            nullable=False,
        ),
        sa.Column("decision_note", sa.String(), nullable=True),
        sa.Column(
            "decided_by_agent_role_id", sa.Uuid(), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["business_id"], ["business.id"]),
        sa.ForeignKeyConstraint(
            ["from_unit_id"], ["business_unit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_unit_id"], ["business_unit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_agent_role_id"], ["agent_role.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "cooperation_proposal", schema=None,
    ) as bop:
        bop.create_index(
            "ix_cooperation_proposal_business_id",
            ["business_id"], unique=False,
        )
        bop.create_index(
            "ix_cooperation_proposal_from_unit_id",
            ["from_unit_id"], unique=False,
        )
        bop.create_index(
            "ix_cooperation_proposal_to_unit_id",
            ["to_unit_id"], unique=False,
        )
        bop.create_index(
            "ix_cooperation_proposal_status",
            ["status"], unique=False,
        )
        bop.create_index(
            "ix_cooperation_proposal_created_at",
            ["created_at"], unique=False,
        )

    op.create_table(
        "cross_unit_query_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("from_unit_id", sa.Uuid(), nullable=False),
        sa.Column("to_unit_id", sa.Uuid(), nullable=False),
        sa.Column(
            "asked_by_agent_role_id", sa.Uuid(), nullable=True,
        ),
        sa.Column("question_summary", sa.String(), nullable=False),
        sa.Column("response_summary", sa.String(), nullable=True),
        sa.Column("asked_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["business.id"]),
        sa.ForeignKeyConstraint(
            ["from_unit_id"], ["business_unit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_unit_id"], ["business_unit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["asked_by_agent_role_id"], ["agent_role.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "cross_unit_query_log", schema=None,
    ) as bop:
        bop.create_index(
            "ix_cross_unit_query_log_business_id",
            ["business_id"], unique=False,
        )
        bop.create_index(
            "ix_cross_unit_query_log_from_unit_id",
            ["from_unit_id"], unique=False,
        )
        bop.create_index(
            "ix_cross_unit_query_log_to_unit_id",
            ["to_unit_id"], unique=False,
        )
        bop.create_index(
            "ix_cross_unit_query_log_asked_at",
            ["asked_at"], unique=False,
        )

    # Postgres-only: extend the action_class enum to include STRATEGIC.
    # SQLite stores enums as TEXT so the new value flows through
    # without schema changes.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE actionclass ADD VALUE IF NOT EXISTS 'STRATEGIC'"
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "cross_unit_query_log", schema=None,
    ) as bop:
        bop.drop_index("ix_cross_unit_query_log_asked_at")
        bop.drop_index("ix_cross_unit_query_log_to_unit_id")
        bop.drop_index("ix_cross_unit_query_log_from_unit_id")
        bop.drop_index("ix_cross_unit_query_log_business_id")
    op.drop_table("cross_unit_query_log")

    with op.batch_alter_table(
        "cooperation_proposal", schema=None,
    ) as bop:
        bop.drop_index("ix_cooperation_proposal_created_at")
        bop.drop_index("ix_cooperation_proposal_status")
        bop.drop_index("ix_cooperation_proposal_to_unit_id")
        bop.drop_index("ix_cooperation_proposal_from_unit_id")
        bop.drop_index("ix_cooperation_proposal_business_id")
    op.drop_table("cooperation_proposal")
    sa.Enum(name="cooperationstatus").drop(
        op.get_bind(), checkfirst=True,
    )
    # Postgres ALTER TYPE ... DROP VALUE is not supported; STRATEGIC
    # enum value persists post-downgrade as a no-op residue.
