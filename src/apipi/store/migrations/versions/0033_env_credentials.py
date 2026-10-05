"""Vault environment_variable credentials (#519).

Revision ID: 0033_env_credentials
Revises: 0032_session_file_paths
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision: str = "0033_env_credentials"
down_revision: str | None = "0032_session_file_paths"
branch_labels: str | None = None
depends_on: str | None = None

JSONType = JSON().with_variant(JSONB(), "postgresql")

_NEW_CHECK = "auth_type IN ('static_bearer', 'environment_variable')"
_OLD_CHECK = "auth_type IN ('static_bearer')"


def upgrade() -> None:
    with op.batch_alter_table("vault_credentials") as batch:
        batch.add_column(sa.Column("secret_name", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("allowed_hosts", JSONType, nullable=True))
        batch.add_column(
            sa.Column("metadata", JSONType, nullable=False, server_default="{}")
        )
        batch.alter_column("mcp_server_url", existing_type=sa.String(), nullable=True)
        batch.drop_constraint("vault_credentials_auth_type_check", type_="check")
        batch.create_check_constraint("vault_credentials_auth_type_check", _NEW_CHECK)
        batch.create_unique_constraint(
            "vault_credentials_secret_name_key",
            ["tenant_id", "vault_id", "secret_name"],
        )


def downgrade() -> None:
    op.execute("DELETE FROM vault_credentials WHERE auth_type = 'environment_variable'")
    with op.batch_alter_table("vault_credentials") as batch:
        batch.drop_constraint("vault_credentials_secret_name_key", type_="unique")
        batch.drop_constraint("vault_credentials_auth_type_check", type_="check")
        batch.create_check_constraint("vault_credentials_auth_type_check", _OLD_CHECK)
        batch.alter_column("mcp_server_url", existing_type=sa.String(), nullable=False)
        batch.drop_column("metadata")
        batch.drop_column("allowed_hosts")
        batch.drop_column("secret_name")
