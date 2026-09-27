"""Store turn failure classification on turn logs.

Revision ID: 0016_turn_failure
Revises: 0015_session_org
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0016_turn_failure"
down_revision: str | None = "0015_session_org"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "turn_logs", sa.Column("failure_source", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "turn_logs", sa.Column("upstream_status", sa.Integer(), nullable=True)
    )
    op.add_column("turn_logs", sa.Column("retryable", sa.Boolean(), nullable=True))
    op.add_column(
        "turn_logs", sa.Column("legacy_code", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("turn_logs", "legacy_code")
    op.drop_column("turn_logs", "retryable")
    op.drop_column("turn_logs", "upstream_status")
    op.drop_column("turn_logs", "failure_source")
