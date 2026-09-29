"""Persist hosted sandbox runtime status on the session.

Revision ID: 0018_sandbox_status
Revises: 0017_upstream_attempts
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0018_sandbox_status"
down_revision: str | None = "0017_upstream_attempts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions", sa.Column("sandbox_state", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "sessions", sa.Column("sandbox_reason", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "sessions",
        sa.Column("sandbox_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("sandbox_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("sessions", sa.Column("sandbox_worker_id", sa.Uuid(), nullable=True))
    op.add_column("sessions", sa.Column("sandbox_image", sa.String(), nullable=True))
    op.add_column(
        "sessions", sa.Column("sandbox_image_version", sa.String(), nullable=True)
    )
    op.add_column(
        "sessions", sa.Column("sandbox_size", sa.String(length=8), nullable=True)
    )
    op.add_column(
        "sessions",
        sa.Column(
            "sandbox_cold_boots", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "sessions", sa.Column("sandbox_last_boot_ms", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("sessions", "sandbox_last_boot_ms")
    op.drop_column("sessions", "sandbox_cold_boots")
    op.drop_column("sessions", "sandbox_size")
    op.drop_column("sessions", "sandbox_image_version")
    op.drop_column("sessions", "sandbox_image")
    op.drop_column("sessions", "sandbox_worker_id")
    op.drop_column("sessions", "sandbox_seen_at")
    op.drop_column("sessions", "sandbox_since")
    op.drop_column("sessions", "sandbox_reason")
    op.drop_column("sessions", "sandbox_state")
