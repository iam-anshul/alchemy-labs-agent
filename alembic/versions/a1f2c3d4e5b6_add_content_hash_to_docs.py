"""add content_hash to docs

Revision ID: a1f2c3d4e5b6
Revises: 44cb34acce18
Create Date: 2026-06-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f2c3d4e5b6'
down_revision: Union[str, Sequence[str], None] = '44cb34acce18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # sha256 hex digest (64 chars) of the original file bytes; nullable so
    # rows ingested before this column existed remain valid (they just won't
    # participate in hash-based dedup).
    op.add_column('docs', sa.Column('content_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('docs', 'content_hash')
