"""Inline session instructions snapshot.

Revision ID: 0011_session_instructions
Revises: 0010_session_model
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0011_session_instructions"
down_revision: str | None = "0010_session_model"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("instructions", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "instructions")
