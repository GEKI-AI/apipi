"""Add agents.revision for model-host attribution.

Revision ID: 0021_agent_revision
Revises: 0020_mcp_list_tools
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0021_agent_revision"
down_revision: str | None = "0020_mcp_list_tools"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=True))
    op.execute("UPDATE agents SET revision = 1 WHERE revision IS NULL")
    with op.batch_alter_table("agents") as batch:
        batch.alter_column("revision", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("revision")
