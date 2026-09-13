"""Session key_id and artifact size for blob keys.

Revision ID: 0008_artifact_blobs
Revises: 0007_artifact_turn_id
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0008_artifact_blobs"
down_revision: str | None = "0007_artifact_turn_id"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("key_id", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "artifacts",
        sa.Column("key_id", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "artifacts",
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("artifacts", "byte_size")
    op.drop_column("artifacts", "key_id")
    op.drop_column("sessions", "key_id")
