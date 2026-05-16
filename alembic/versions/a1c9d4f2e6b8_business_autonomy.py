"""business.autonomy_mode + daily_max_iterations

Revision ID: a1c9d4f2e6b8
Revises: 8e2a5f9c1064
Create Date: 2026-05-15

Adds the per-business autonomy knobs so Mike can choose whether the
team auto-pulls BACKLOG cards into work (and what stops it). Daily +
monthly $ caps continue to live on BudgetPolicy — this migration only
adds the *mode* selector and the iteration cap (which BudgetPolicy
doesn't cover, since it counts dollars not card-fires).

Both columns are nullable so existing rows backfill as ``mode=NULL``
which the app treats as ``off`` (the safe default — no autonomy until
Mike opts in).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c9d4f2e6b8"
down_revision: Union[str, Sequence[str], None] = "8e2a5f9c1064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("business", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("autonomy_mode", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("daily_max_iterations", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("business", schema=None) as batch_op:
        batch_op.drop_column("daily_max_iterations")
        batch_op.drop_column("autonomy_mode")
