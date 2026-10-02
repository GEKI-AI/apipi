"""Web search usage counters and the web_search_call item type (#478).

Revision ID: 0028_search_usage
Revises: 0027_artifact_uploads
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

from apipi.store.models import JSONType

revision: str = "0028_search_usage"
down_revision: str | None = "0027_artifact_uploads"
branch_labels: str | None = None
depends_on: str | None = None

_NEW_CHECK = (
    "type IN ('message', 'function_call', 'mcp_call', "
    "'mcp_list_tools', 'command_execution', 'web_search_call')"
)
_OLD_CHECK = (
    "type IN ('message', 'function_call', 'mcp_call', "
    "'mcp_list_tools', 'command_execution')"
)


def upgrade() -> None:
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("items_type_check", type_="check")
        batch.create_check_constraint("items_type_check", _NEW_CHECK)
    for table in ("turn_logs", "usage_rollups"):
        op.add_column(
            table,
            sa.Column("search_calls", sa.Integer(), nullable=False, server_default="0"),
        )
        op.add_column(
            table,
            sa.Column("search_units", sa.Integer(), nullable=False, server_default="0"),
        )
    op.add_column(
        "turn_logs",
        sa.Column("search_counts", JSONType, nullable=False, server_default="{}"),
    )
    op.create_table(
        "search_turn_counts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("key_source", sa.String(length=16), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["sessions.tenant_id", "sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "turn_id"],
            ["turns.tenant_id", "turns.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "turn_id", "provider", "key_source"),
    )


def downgrade() -> None:
    op.drop_table("search_turn_counts")
    with op.batch_alter_table("turn_logs") as batch:
        batch.drop_column("search_counts")
        batch.drop_column("search_units")
        batch.drop_column("search_calls")
    with op.batch_alter_table("usage_rollups") as batch:
        batch.drop_column("search_units")
        batch.drop_column("search_calls")
    op.execute("DELETE FROM items WHERE type = 'web_search_call'")
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("items_type_check", type_="check")
        batch.create_check_constraint("items_type_check", _OLD_CHECK)
