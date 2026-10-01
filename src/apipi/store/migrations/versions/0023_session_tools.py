"""Store inline session tools so turns rebuild MCP servers without a probe.

Agent sessions read the live agent definition at turn start; agent-less
sessions keep the tools from session create here.

Revision ID: 0023_session_tools
Revises: 0022_drop_agent_versions
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

from apipi.store.models import JSONType

revision: str = "0023_session_tools"
down_revision: str | None = "0022_drop_agent_versions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("tools", JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("tools")
