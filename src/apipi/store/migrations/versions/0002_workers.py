"""Worker registry and session lease columns.

Revision ID: 0002_workers
Revises: 0001_initial
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002_workers"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("sessions", sa.Column("worker_id", sa.Uuid(), nullable=True))
    op.add_column("sessions", sa.Column("lease_id", sa.Uuid(), nullable=True))
    op.add_column(
        "sessions", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("sessions", "lease_until")
    op.drop_column("sessions", "lease_id")
    op.drop_column("sessions", "worker_id")
    op.drop_table("workers")
