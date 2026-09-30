"""Allow mcp_list_tools output items.

Revision ID: 0020_mcp_list_tools
Revises: 0019_agent_versions
Create Date: 2026-09-30
"""

from alembic import op

revision: str = "0020_mcp_list_tools"
down_revision: str | None = "0019_agent_versions"
branch_labels: str | None = None
depends_on: str | None = None

_NEW_CHECK = (
    "type IN ('message', 'function_call', 'mcp_call', "
    "'mcp_list_tools', 'command_execution')"
)
_OLD_CHECK = "type IN ('message', 'function_call', 'mcp_call', 'command_execution')"


def upgrade() -> None:
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("items_type_check", type_="check")
        batch.create_check_constraint("items_type_check", _NEW_CHECK)


def downgrade() -> None:
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("items_type_check", type_="check")
        batch.create_check_constraint("items_type_check", _OLD_CHECK)
