"""Usage store depth: turn log metadata and daily rollups.

Revision ID: 0009_usage_store
Revises: 0008_artifact_blobs
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0009_usage_store"
down_revision: str | None = "0008_artifact_blobs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "turn_logs",
        sa.Column("key_id", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "turn_logs",
        sa.Column(
            "environment_type", sa.String(length=32), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "turn_logs",
        sa.Column("run_mode", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "turn_logs",
        sa.Column("instance_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "turn_logs",
        sa.Column("artifact_bytes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "usage_rollups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("artifact_bytes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "day"),
    )


def downgrade() -> None:
    op.drop_table("usage_rollups")
    op.drop_column("turn_logs", "artifact_bytes")
    op.drop_column("turn_logs", "instance_id")
    op.drop_column("turn_logs", "run_mode")
    op.drop_column("turn_logs", "environment_type")
    op.drop_column("turn_logs", "key_id")
