"""Persist auth user_id on sessions.

Revision ID: 0012_session_user
Revises: 0011_idle_ttl
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0012_session_user"
down_revision: str | None = "0011_idle_ttl"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("user_id", sa.String(), nullable=True))
    op.create_index("ix_sessions_tenant_user", "sessions", ["tenant_id", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_tenant_user", table_name="sessions")
    op.drop_column("sessions", "user_id")
