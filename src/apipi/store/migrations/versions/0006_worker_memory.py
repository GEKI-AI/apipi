"""Worker RAM budget.

Revision ID: 0006_worker_memory
Revises: 0005_leases
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0006_worker_memory"
down_revision: str | None = "0005_leases"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column("memory_mb", sa.Integer(), nullable=False, server_default="16384"),
    )


def downgrade() -> None:
    op.drop_column("workers", "memory_mb")
