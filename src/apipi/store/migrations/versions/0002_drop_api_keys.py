"""Drop api_keys. Auth does not store secrets.

Revision ID: 0002_drop_api_keys
Revises: 0001_initial
Create Date: 2026-09-12
"""

from alembic import op

revision: str = "0002_drop_api_keys"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("api_keys")


def downgrade() -> None:
    raise NotImplementedError
