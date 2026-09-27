"""Session defaults on agents.

Revision ID: 0013_agent_session_defaults
Revises: 0012_session_user
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

from apipi.services.session_defaults import sandbox_defaults_from_metadata
from apipi.store.models import JSONType

revision: str = "0013_agent_session_defaults"
down_revision: str | None = "0012_session_user"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("session_defaults", JSONType, nullable=True))
    conn = op.get_bind()
    agents = sa.table(
        "agents",
        sa.column("id"),
        sa.column("tenant_id"),
        sa.column("metadata"),
        sa.column("session_defaults"),
    )
    rows = conn.execute(sa.select(agents.c.id, agents.c.tenant_id, agents.c.metadata))
    for row in rows:
        defaults = sandbox_defaults_from_metadata(row.metadata)
        if defaults is None:
            continue
        conn.execute(
            agents.update()
            .where(agents.c.id == row.id, agents.c.tenant_id == row.tenant_id)
            .values(session_defaults=defaults)
        )


def downgrade() -> None:
    op.drop_column("agents", "session_defaults")
