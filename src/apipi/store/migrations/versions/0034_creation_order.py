"""Index turns, items, and artifacts by session and creation stamp (#555).

Revision ID: 0034_creation_order
Revises: 0033_env_credentials
Create Date: 2026-10-05
"""

from alembic import op

revision: str = "0034_creation_order"
down_revision: str | None = "0033_env_credentials"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_turns_session_created", "turns", ["tenant_id", "session_id", "created_at"]
    )
    op.create_index(
        "ix_items_session_created", "items", ["tenant_id", "session_id", "created_at"]
    )
    op.create_index(
        "ix_artifacts_session_created",
        "artifacts",
        ["tenant_id", "session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_session_created", table_name="artifacts")
    op.drop_index("ix_items_session_created", table_name="items")
    op.drop_index("ix_turns_session_created", table_name="turns")
