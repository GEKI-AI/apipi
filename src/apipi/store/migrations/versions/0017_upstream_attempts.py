"""Store model attempt counts on turn logs.

Revision ID: 0017_upstream_attempts
Revises: 0016_turn_failure
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0017_upstream_attempts"
down_revision: str | None = "0016_turn_failure"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "turn_logs", sa.Column("upstream_attempts", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("turn_logs", "upstream_attempts")
