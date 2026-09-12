"""Session required_actions for function tools.

Revision ID: 0003_session_required_actions
Revises: 0002_drop_api_keys
Create Date: 2026-09-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_session_required_actions"
down_revision: str | None = "0002_drop_api_keys"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "required_actions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("sessions", "required_actions", server_default=None)


def downgrade() -> None:
    op.drop_column("sessions", "required_actions")
