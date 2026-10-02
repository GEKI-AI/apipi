"""Drop the duplicated Pi session URI; the blob id is the only source.

Revision ID: 0024_drop_pi_session_uri
Revises: 0023_session_tools
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0024_drop_pi_session_uri"
down_revision: str | None = "0023_session_tools"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("pi_session_uri")


def downgrade() -> None:
    op.add_column("sessions", sa.Column("pi_session_uri", sa.String(), nullable=True))
