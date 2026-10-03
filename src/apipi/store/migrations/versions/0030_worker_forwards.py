"""Fleet placement columns on workers and the worker_forwards mailbox (#490).

Revision ID: 0030_worker_forwards
Revises: 0029_upload_request_id
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision: str = "0030_worker_forwards"
down_revision: str | None = "0029_upload_request_id"
branch_labels: str | None = None
depends_on: str | None = None

JSONType = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("workers", sa.Column("accepts", JSONType, nullable=True))
    op.add_column("workers", sa.Column("images", JSONType, nullable=True))
    op.add_column("workers", sa.Column("arch", sa.String(length=32), nullable=True))
    op.add_column(
        "workers",
        sa.Column(
            "draining",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_table(
        "worker_forwards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("op", sa.String(length=32), nullable=False),
        sa.Column("wait", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("origin", sa.String(length=128), nullable=False),
        sa.Column("body", JSONType, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worker_forwards_target_status",
        "worker_forwards",
        ["target", "status"],
    )
    op.create_index("ix_worker_forwards_created_at", "worker_forwards", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_forwards_created_at", table_name="worker_forwards")
    op.drop_index("ix_worker_forwards_target_status", table_name="worker_forwards")
    op.drop_table("worker_forwards")
    op.drop_column("workers", "draining")
    op.drop_column("workers", "arch")
    op.drop_column("workers", "images")
    op.drop_column("workers", "accepts")
