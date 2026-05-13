"""business.founder_brief — Day-0 intake JSON column

Revision ID: e15960c6e32e
Revises: e3ef525a92e1
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
import sqlmodel  # noqa: F401  -- SQLModel types referenced in generated columns

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


revision: str = "e15960c6e32e"
down_revision: Union[str, Sequence[str], None] = "e3ef525a92e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("business", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "founder_brief",
                _JSON,
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("business", schema=None) as batch_op:
        batch_op.drop_column("founder_brief")
