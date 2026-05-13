"""shared_resource + shared_resource_usage tables

Revision ID: 7d3f9c4e1820
Revises: 4c1e8a2d9b07
Create Date: 2026-05-12

PR5 — SharedResource model for company-wide infrastructure (AI mesh,
OAuth CLIs, shared accounts). Usage attribution log per consumer unit.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '7d3f9c4e1820'
down_revision: Union[str, Sequence[str], None] = '4c1e8a2d9b07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_col() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "shared_resource",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "AI_MODEL", "COMPUTE", "DOMAIN_POOL", "HOST_POOL",
                "SHARED_ACCOUNT", "OAUTH_CLI",
                name="sharedresourcekind",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("host_business_unit_id", sa.Uuid(), nullable=True),
        sa.Column("endpoint", sa.String(), nullable=True),
        sa.Column("config", _json_col(), nullable=False),
        sa.Column("cost_model", sa.String(), nullable=False),
        sa.Column("fixed_monthly_cost_usd", sa.Float(), nullable=True),
        sa.Column("available_in_modes", _json_col(), nullable=False),
        sa.Column("quota_window_seconds", sa.Integer(), nullable=True),
        sa.Column("quota_limit_in_window", sa.Integer(), nullable=True),
        sa.Column("quota_calls_in_window", sa.Integer(), nullable=False),
        sa.Column("quota_window_started_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["business.id"]),
        sa.ForeignKeyConstraint(
            ["host_business_unit_id"], ["business_unit.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("shared_resource", schema=None) as bop:
        bop.create_index(
            "ix_shared_resource_business_id",
            ["business_id"], unique=False,
        )
        bop.create_index(
            "ix_shared_resource_kind", ["kind"], unique=False,
        )
        bop.create_index(
            "ix_shared_resource_name", ["name"], unique=False,
        )
        bop.create_index(
            "ix_shared_resource_host_business_unit_id",
            ["host_business_unit_id"], unique=False,
        )
        bop.create_index(
            "ix_shared_resource_is_active",
            ["is_active"], unique=False,
        )

    op.create_table(
        "shared_resource_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_unit_id", sa.Uuid(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=False),
        sa.Column("units_consumed", sa.Float(), nullable=False),
        sa.Column("cost_attributed_usd", sa.Float(), nullable=False),
        sa.Column("skill_name", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["resource_id"], ["shared_resource.id"],
        ),
        sa.ForeignKeyConstraint(
            ["consumer_unit_id"], ["business_unit.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "shared_resource_usage", schema=None,
    ) as bop:
        bop.create_index(
            "ix_shared_resource_usage_resource_id",
            ["resource_id"], unique=False,
        )
        bop.create_index(
            "ix_shared_resource_usage_consumer_unit_id",
            ["consumer_unit_id"], unique=False,
        )
        bop.create_index(
            "ix_shared_resource_usage_used_at",
            ["used_at"], unique=False,
        )
        bop.create_index(
            "ix_shared_resource_usage_skill_name",
            ["skill_name"], unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "shared_resource_usage", schema=None,
    ) as bop:
        bop.drop_index("ix_shared_resource_usage_skill_name")
        bop.drop_index("ix_shared_resource_usage_used_at")
        bop.drop_index("ix_shared_resource_usage_consumer_unit_id")
        bop.drop_index("ix_shared_resource_usage_resource_id")
    op.drop_table("shared_resource_usage")

    with op.batch_alter_table("shared_resource", schema=None) as bop:
        bop.drop_index("ix_shared_resource_is_active")
        bop.drop_index("ix_shared_resource_host_business_unit_id")
        bop.drop_index("ix_shared_resource_name")
        bop.drop_index("ix_shared_resource_kind")
        bop.drop_index("ix_shared_resource_business_id")
    op.drop_table("shared_resource")
    sa.Enum(name="sharedresourcekind").drop(
        op.get_bind(), checkfirst=True,
    )
