"""Artifact metadata.

Revision ID: 0004_artifacts
Revises: 0003_session_required_actions
Create Date: 2026-09-12
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0004_artifacts"
down_revision: str | None = "0003_session_required_actions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["sessions.tenant_id", "sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )


def downgrade() -> None:
    op.drop_table("artifacts")
