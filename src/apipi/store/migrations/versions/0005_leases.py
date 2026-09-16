"""Lease indexes and workers.api_instance_id.

Revision ID: 0005_leases
Revises: 0004_vaults
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0005_leases"
down_revision: str | None = "0004_vaults"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "workers", sa.Column("api_instance_id", sa.String(128), nullable=True)
    )
    op.create_index(
        "ix_sessions_lease_until",
        "sessions",
        ["lease_until"],
        postgresql_where=sa.text("lease_id IS NOT NULL"),
        sqlite_where=sa.text("lease_id IS NOT NULL"),
    )
    op.create_index(
        "ix_sessions_worker_id",
        "sessions",
        ["worker_id"],
        postgresql_where=sa.text("worker_id IS NOT NULL"),
        sqlite_where=sa.text("worker_id IS NOT NULL"),
    )
    op.create_index(
        "uq_sessions_lease_id",
        "sessions",
        ["lease_id"],
        unique=True,
        postgresql_where=sa.text("lease_id IS NOT NULL"),
        sqlite_where=sa.text("lease_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_sessions_lease_id", table_name="sessions")
    op.drop_index("ix_sessions_worker_id", table_name="sessions")
    op.drop_index("ix_sessions_lease_until", table_name="sessions")
    op.drop_column("workers", "api_instance_id")
