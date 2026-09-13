"""Optional turn_id on published artifacts.

Revision ID: 0007_artifact_turn_id
Revises: 0006_turn_logs
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0007_artifact_turn_id"
down_revision: str | None = "0006_turn_logs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("turn_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifacts", "turn_id")
