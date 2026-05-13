"""external_service_account table — unified per-unit credential storage

Revision ID: 4c1e8a2d9b07
Revises: 9a2f1b7c8e30
Create Date: 2026-05-12

PR4 — adds ExternalServiceAccount (LLM + non-LLM credentials scoped to
BusinessUnit). The existing ProviderAccount dataclass (in
korpha.inference.registry) stays in place for LLM inference routing
short-term; future PRs migrate inference onto this unified shape.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '4c1e8a2d9b07'
down_revision: Union[str, Sequence[str], None] = '9a2f1b7c8e30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_col() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


_SERVICE_KINDS = (
    "LLM_OPENAI_COMPAT", "LLM_ANTHROPIC", "LLM_GOOGLE",
    "IMAGE_GEN", "TTS", "STT",
    "STRIPE", "PAYPAL", "LEMON_SQUEEZY", "PADDLE",
    "JVZOO", "WARRIOR_PLUS",
    "RESEND", "SENDGRID", "MAILGUN", "POSTMARK",
    "KDP_API", "PRINTFUL", "PRINTIFY", "ETSY", "GUMROAD",
    "TEACHABLE", "THINKIFIC", "KAJABI",
    "CONVERTKIT", "BEEHIIV", "MAILERLITE", "GETRESPONSE", "AWEBER",
    "CLOUDFLARE", "VERCEL", "FLY", "RAILWAY",
    "VPS_HOST", "DOMAIN_REGISTRAR", "SUPABASE", "NEON",
    "CUSTOM",
)


def upgrade() -> None:
    op.create_table(
        "external_service_account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("business_unit_id", sa.Uuid(), nullable=True),
        sa.Column(
            "service",
            sa.Enum(*_SERVICE_KINDS, name="externalservicekind"),
            nullable=False,
        ),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("credentials_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("provider_meta", _json_col(), nullable=False),
        sa.Column(
            "spending_cap_usd_per_month",
            sa.Numeric(precision=12, scale=4), nullable=True,
        ),
        sa.Column(
            "spending_used_this_month_usd",
            sa.Numeric(precision=12, scale=4), nullable=False,
        ),
        sa.Column("spending_cap_resets_at", sa.DateTime(), nullable=True),
        sa.Column("rate_limit_meta", _json_col(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_message", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["business.id"]),
        sa.ForeignKeyConstraint(
            ["business_unit_id"], ["business_unit.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "external_service_account", schema=None,
    ) as batch_op:
        batch_op.create_index(
            "ix_external_service_account_business_id",
            ["business_id"], unique=False,
        )
        batch_op.create_index(
            "ix_external_service_account_business_unit_id",
            ["business_unit_id"], unique=False,
        )
        batch_op.create_index(
            "ix_external_service_account_service",
            ["service"], unique=False,
        )
        batch_op.create_index(
            "ix_external_service_account_is_active",
            ["is_active"], unique=False,
        )
        # Composite index for the resolver hot path
        batch_op.create_index(
            "ix_esa_unit_service_active",
            ["business_unit_id", "service", "is_active"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "external_service_account", schema=None,
    ) as batch_op:
        batch_op.drop_index("ix_esa_unit_service_active")
        batch_op.drop_index("ix_external_service_account_is_active")
        batch_op.drop_index("ix_external_service_account_service")
        batch_op.drop_index(
            "ix_external_service_account_business_unit_id"
        )
        batch_op.drop_index("ix_external_service_account_business_id")
    op.drop_table("external_service_account")
    sa.Enum(name="externalservicekind").drop(
        op.get_bind(), checkfirst=True,
    )
