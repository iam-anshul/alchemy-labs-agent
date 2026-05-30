"""add status to docs

Revision ID: b1c2d3e4f5a6
Revises: af6d0b0c9fe0
Create Date: 2026-05-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "af6d0b0c9fe0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("docs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(), nullable=False, server_default="ready"))


def downgrade() -> None:
    with op.batch_alter_table("docs", schema=None) as batch_op:
        batch_op.drop_column("status")
