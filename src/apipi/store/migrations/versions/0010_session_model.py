"""Inline session model snapshot.

Revision ID: 0010_session_model
Revises: 0009_usage_store
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0010_session_model"
down_revision: str | None = "0009_usage_store"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("model", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "model")
