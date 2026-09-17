"""Hosted skill package metadata.

Revision ID: 0008_skills
Revises: 0007_files
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0008_skills"
down_revision: str | None = "0007_files"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )


def downgrade() -> None:
    op.drop_table("skills")
