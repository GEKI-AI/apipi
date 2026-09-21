"""Full URI for the harness Pi session cache.

Revision ID: 0009_pi_session_uri
Revises: 0008_skills
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0009_pi_session_uri"
down_revision: str | None = "0008_skills"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("pi_session_uri", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "pi_session_uri")
