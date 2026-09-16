"""Pi session cache blob pointer on sessions.

Revision ID: 0003_pi_session
Revises: 0002_workers
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pi_session"
down_revision: str | None = "0002_workers"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("pi_session_id", sa.Uuid(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("pi_session_bytes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "pi_session_bytes")
    op.drop_column("sessions", "pi_session_id")
