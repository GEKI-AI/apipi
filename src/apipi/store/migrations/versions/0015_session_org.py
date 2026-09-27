"""Persist auth org_id on sessions.

Revision ID: 0015_session_org
Revises: 0014_templates
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0015_session_org"
down_revision: str | None = "0014_templates"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("org_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "org_id")
