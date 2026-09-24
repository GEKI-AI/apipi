"""Optional idle TTL on agents and sessions.

Revision ID: 0011_idle_ttl
Revises: 0010_uploads
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0011_idle_ttl"
down_revision: str | None = "0010_uploads"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("idle_ttl", sa.String(length=32), nullable=True))
    op.add_column(
        "sessions", sa.Column("idle_ttl", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("sessions", "idle_ttl")
    op.drop_column("agents", "idle_ttl")
