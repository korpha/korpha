"""business_unit + business_product tables + default-unit backfill

Revision ID: eb2e487c5ec8
Revises: c4f8a3e1b9d2
Create Date: 2026-05-12

Adds the two new tables from PR1 (BusinessUnit recursive tree + Product
leaf) and backfills a default BusinessUnit for every existing Business
so the rest of the org model (FK additions in PR3, resolver in PR4,
etc.) has something to reference.

Backfill is **idempotent** — safe to re-run after partial failure. The
script checks for an existing default unit per Business before creating
one, so applying this migration twice is a no-op on the second run.

NOTE: Pre-existing tables that aren't in alembic history (founder_note,
revenue_event, budget_policy, kanban_card et al, plus columns added to
approval in recent PRs) are intentionally NOT included here. They land
via ``SQLModel.metadata.create_all()`` at app startup today and have
their own follow-up migration story.
"""
from __future__ import annotations

from typing import Sequence, Union
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'eb2e487c5ec8'
down_revision: Union[str, Sequence[str], None] = 'c4f8a3e1b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# JSON variant — JSONB on Postgres, plain JSON on SQLite. Matches the
# convention in korpha/db/_base.py::json_column.
def _json_col() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema + backfill default units."""

    # ---- business_unit table ----
    op.create_table(
        'business_unit',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('business_id', sa.Uuid(), nullable=False),
        sa.Column('parent_id', sa.Uuid(), nullable=True),
        sa.Column(
            'kind',
            sa.Enum(
                'DEFAULT', 'LINE', 'TYPE', 'SERIES', 'NICHE',
                'AUDIENCE', 'PRODUCT_VP', 'CUSTOM',
                name='businessunitkind',
            ),
            nullable=False,
        ),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('owner_agent_role_id', sa.Uuid(), nullable=True),
        sa.Column('playbook_skill_pack', sa.String(), nullable=True),
        sa.Column('niche_profile', _json_col(), nullable=True),
        sa.Column('memory_namespace_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('paused_at', sa.DateTime(), nullable=True),
        sa.Column('paused_reason', sa.String(), nullable=True),
        sa.Column('config', _json_col(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['business_id'], ['business.id']),
        sa.ForeignKeyConstraint(
            ['owner_agent_role_id'], ['agent_role.id'],
        ),
        sa.ForeignKeyConstraint(['parent_id'], ['business_unit.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'business_id', 'parent_id', 'slug',
            name='business_unit_sibling_slug_unique',
        ),
    )
    with op.batch_alter_table('business_unit', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_business_unit_business_id'),
            ['business_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_unit_kind'),
            ['kind'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_unit_memory_namespace_id'),
            ['memory_namespace_id'], unique=True,
        )
        batch_op.create_index(
            batch_op.f('ix_business_unit_owner_agent_role_id'),
            ['owner_agent_role_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_unit_parent_id'),
            ['parent_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_unit_slug'),
            ['slug'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_unit_status'),
            ['status'], unique=False,
        )

    # ---- business_product table ----
    op.create_table(
        'business_product',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('business_unit_id', sa.Uuid(), nullable=False),
        sa.Column('business_id', sa.Uuid(), nullable=False),
        sa.Column(
            'kind',
            sa.Enum(
                'BOOK', 'DESIGN', 'COURSE', 'EBOOK', 'NEWSLETTER',
                'MEMBERSHIP', 'SAAS_APP', 'CAMPAIGN', 'SERVICE',
                'CUSTOM',
                name='productkind',
            ),
            nullable=False,
        ),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('starts_at', sa.DateTime(), nullable=True),
        sa.Column('ends_at', sa.DateTime(), nullable=True),
        sa.Column('attributes', _json_col(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['business_id'], ['business.id']),
        sa.ForeignKeyConstraint(
            ['business_unit_id'], ['business_unit.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'business_unit_id', 'slug',
            name='business_product_unit_slug_unique',
        ),
    )
    with op.batch_alter_table('business_product', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_business_product_business_id'),
            ['business_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_product_business_unit_id'),
            ['business_unit_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_product_kind'),
            ['kind'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_product_slug'),
            ['slug'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_business_product_status'),
            ['status'], unique=False,
        )

    # ---- backfill default BusinessUnit per existing Business ----
    backfill_default_units(op.get_bind())


def backfill_default_units(conn) -> int:
    """Idempotent default-unit backfill. Returns count of units created.

    Each existing Business gets exactly one default BusinessUnit. Safe
    to re-run after partial failure — checks for existing rows first.

    Exposed at module scope so the tests + a future
    ``korpha business backfill-units`` CLI can re-trigger it without
    rerunning the whole alembic chain.

    Uses raw SQL rather than ORM models so the migration stays
    reproducible against the schema snapshot it was authored for — even
    if the model definitions change in future PRs.
    """
    import json
    import re
    from datetime import UTC, datetime

    def _slugify(text: str) -> str:
        s = re.sub(r"[^a-z0-9-]+", "-", text.strip().lower())
        s = re.sub(r"-+", "-", s).strip("-")
        return s[:60] or "unit"

    now = datetime.now(UTC)
    created = 0

    businesses = conn.execute(
        sa.text(
            "SELECT b.id, b.name FROM business b "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM business_unit u WHERE u.business_id = b.id"
            ")"
        )
    ).fetchall()

    # SQLAlchemy's Uuid type maps differently per dialect:
    # SQLite stores as CHAR(32) hex-stripped, Postgres uses native UUID.
    # Raw SQL bypasses type conversion, so normalize to str(UUID) and
    # let the driver bind it as TEXT — both dialects accept that.
    dialect_name = conn.dialect.name

    def _uuid_param(u) -> object:
        if dialect_name == "postgresql":
            return u  # native binding fine
        # SQLite: hex without dashes is what Uuid() persists
        return str(u).replace("-", "")

    for biz_id, biz_name in businesses:
        unit_id = uuid4()
        namespace_id = uuid4()
        slug = _slugify(str(biz_name or "unit"))

        existing = conn.execute(
            sa.text(
                "SELECT 1 FROM business_unit "
                "WHERE business_id = :bid AND parent_id IS NULL "
                "AND slug = :slug"
            ),
            {"bid": _uuid_param(biz_id), "slug": slug},
        ).first()
        if existing:
            continue

        conn.execute(
            sa.text(
                "INSERT INTO business_unit ("
                "  id, business_id, parent_id, kind, name, slug, "
                "  owner_agent_role_id, playbook_skill_pack, "
                "  niche_profile, memory_namespace_id, "
                "  status, paused_at, paused_reason, config, "
                "  created_at, updated_at"
                ") VALUES ("
                "  :id, :business_id, NULL, :kind, :name, :slug, "
                "  NULL, NULL, NULL, :namespace_id, "
                "  :status, NULL, NULL, :config, :now, :now"
                ")"
            ),
            {
                "id": _uuid_param(unit_id),
                "business_id": _uuid_param(biz_id),
                "kind": "DEFAULT",
                "name": str(biz_name or "Default"),
                "slug": slug,
                "namespace_id": _uuid_param(namespace_id),
                "status": "active",
                "config": json.dumps({}),
                "now": now,
            },
        )
        created += 1

    return created


def downgrade() -> None:
    """Drop both tables. Backfilled rows go with them."""
    with op.batch_alter_table('business_product', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_business_product_status'))
        batch_op.drop_index(batch_op.f('ix_business_product_slug'))
        batch_op.drop_index(batch_op.f('ix_business_product_kind'))
        batch_op.drop_index(
            batch_op.f('ix_business_product_business_unit_id')
        )
        batch_op.drop_index(batch_op.f('ix_business_product_business_id'))
    op.drop_table('business_product')

    with op.batch_alter_table('business_unit', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_business_unit_status'))
        batch_op.drop_index(batch_op.f('ix_business_unit_slug'))
        batch_op.drop_index(batch_op.f('ix_business_unit_parent_id'))
        batch_op.drop_index(
            batch_op.f('ix_business_unit_owner_agent_role_id')
        )
        batch_op.drop_index(
            batch_op.f('ix_business_unit_memory_namespace_id')
        )
        batch_op.drop_index(batch_op.f('ix_business_unit_kind'))
        batch_op.drop_index(batch_op.f('ix_business_unit_business_id'))
    op.drop_table('business_unit')

    sa.Enum(name='businessunitkind').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='productkind').drop(op.get_bind(), checkfirst=True)
