"""routine + wakeup: tier_override + provider_label columns

Per-routine inference routing override. A routine can pin its LLM
calls to a specific tier (e.g. force Pro for the weekly review even
if the skill defaults to Workhorse) and/or a specific provider
account label (e.g. nightly summarizer always uses cheap-api-account
so subscription quota is preserved for chat work).

Both fields are nullable + default None — existing routines + wakeups
remain identical to before this migration.

Revision ID: c4f8a3e1b9d2
Revises: e15960c6e32e
Create Date: 2026-05-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401


revision: str = "c4f8a3e1b9d2"
down_revision: Union[str, Sequence[str], None] = "e15960c6e32e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("routine", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tier_override", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provider_label", sa.String(), nullable=True))

    with op.batch_alter_table("wakeup", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tier_override", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provider_label", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("wakeup", schema=None) as batch_op:
        batch_op.drop_column("provider_label")
        batch_op.drop_column("tier_override")

    with op.batch_alter_table("routine", schema=None) as batch_op:
        batch_op.drop_column("provider_label")
        batch_op.drop_column("tier_override")
