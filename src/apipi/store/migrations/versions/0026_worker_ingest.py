"""Worker protocol v2 durable ingest: ack cursor and idempotency ledger.

Revision ID: 0026_worker_ingest
Revises: 0025_worker_tokens
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0026_worker_ingest"
down_revision: str | None = "0025_worker_tokens"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("worker_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "worker_ingest",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("worker_seq", sa.Integer(), nullable=False),
        sa.Column("envelope_type", sa.String(64), nullable=False),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("session_id", "worker_seq"),
    )


def downgrade() -> None:
    op.drop_table("worker_ingest")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("worker_seq")
