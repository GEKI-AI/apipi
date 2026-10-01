"""Drop agent versions, snapshots, and the agent revision counter.

The live agent row is the only agent state. Snapshots are deleted;
export any you need with the agent bundle export before upgrading.

Revision ID: 0022_drop_agent_versions
Revises: 0021_agent_revision
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

from apipi.store.models import JSONType

revision: str = "0022_drop_agent_versions"
down_revision: str | None = "0021_agent_revision"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("agent_versions")
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("version_seq")
        batch.drop_column("revision")


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(
            sa.Column("version_seq", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
        )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("definition", JSONType, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "agent_id", "number"),
        sa.CheckConstraint("number >= 1", name="agent_versions_number_check"),
    )
