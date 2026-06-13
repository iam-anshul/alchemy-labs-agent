"""add produced_artifacts to workspace_runs

Revision ID: b2c3d4e5f6a7
Revises: a1f2c3d4e5b6
Create Date: 2026-06-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1f2c3d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Produced files of a run's completed tasks, stored as a JSON array of
    # {rel_path, content_b64, bytes, task_id}. Nullable so existing rows stay
    # valid (they just have no carryable artifacts).
    op.add_column(
        'workspace_runs',
        sa.Column('produced_artifacts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('workspace_runs', 'produced_artifacts')
