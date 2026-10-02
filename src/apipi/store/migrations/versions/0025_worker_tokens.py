"""Per-worker bearer tokens for `/internal/worker`.

Revision ID: 0025_worker_tokens
Revises: 0024_drop_pi_session_uri
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0025_worker_tokens"
down_revision: str | None = "0024_drop_pi_session_uri"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "worker_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_worker_tokens_worker_id", "worker_tokens", ["worker_id"])


def downgrade() -> None:
    op.drop_index("ix_worker_tokens_worker_id", table_name="worker_tokens")
    op.drop_table("worker_tokens")
