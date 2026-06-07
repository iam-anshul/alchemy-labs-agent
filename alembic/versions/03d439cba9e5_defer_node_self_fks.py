"""defer node self-referential FKs

Make nodes.parent_id and pages.node_id DEFERRABLE INITIALLY DEFERRED so a
document's node tree can be inserted in one transaction without ordering the
inserts parent-first. The FK is still enforced — it's validated over the full,
consistent row set at commit instead of per row at insert.

Revision ID: 03d439cba9e5
Revises: cbe9dee89e18
Create Date: 2026-06-07
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '03d439cba9e5'
down_revision: Union[str, Sequence[str], None] = 'cbe9dee89e18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Recreate the two self-referential FKs as DEFERRABLE INITIALLY DEFERRED."""
    op.drop_constraint('nodes_parent_id_fkey', 'nodes', type_='foreignkey')
    op.create_foreign_key(
        'nodes_parent_id_fkey', 'nodes', 'nodes',
        ['parent_id'], ['node_id'],
        ondelete='SET NULL',
        deferrable=True, initially='DEFERRED',
    )

    op.drop_constraint('pages_node_id_fkey', 'pages', type_='foreignkey')
    op.create_foreign_key(
        'pages_node_id_fkey', 'pages', 'nodes',
        ['node_id'], ['node_id'],
        ondelete='SET NULL',
        deferrable=True, initially='DEFERRED',
    )


def downgrade() -> None:
    """Restore the FKs as NOT DEFERRABLE (the default)."""
    op.drop_constraint('pages_node_id_fkey', 'pages', type_='foreignkey')
    op.create_foreign_key(
        'pages_node_id_fkey', 'pages', 'nodes',
        ['node_id'], ['node_id'],
        ondelete='SET NULL',
    )

    op.drop_constraint('nodes_parent_id_fkey', 'nodes', type_='foreignkey')
    op.create_foreign_key(
        'nodes_parent_id_fkey', 'nodes', 'nodes',
        ['parent_id'], ['node_id'],
        ondelete='SET NULL',
    )