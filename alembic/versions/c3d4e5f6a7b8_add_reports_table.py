"""add reports table

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-05-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("report_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("target_length", sa.String(), server_default="standard", nullable=False),
        sa.Column("status", sa.String(), server_default="queued", nullable=False),
        sa.Column("outline_json", sa.JSON(), nullable=True),
        sa.Column("draft_md", sa.Text(), nullable=True),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("n_sections", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_words", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_hops", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("report_id"),
    )
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.create_index(
            "idx_reports_ws_user",
            ["workspace_id", "user_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.drop_index("idx_reports_ws_user")
    op.drop_table("reports")
