"""Hosted Files API metadata.

Revision ID: 0007_files
Revises: 0006_worker_memory
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0007_files"
down_revision: str | None = "0006_worker_memory"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('user_data', 'assistants')",
            name="files_purpose_check",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )


def downgrade() -> None:
    op.drop_table("files")
