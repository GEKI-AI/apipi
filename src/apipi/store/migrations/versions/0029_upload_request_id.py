"""Store the worker request id with each artifact upload slot (#488).

Revision ID: 0029_upload_request_id
Revises: 0028_search_usage
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0029_upload_request_id"
down_revision: str | None = "0028_search_usage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("artifact_uploads", sa.Column("request_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_artifact_uploads_session_request",
        "artifact_uploads",
        ["session_id", "request_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_uploads_session_request", table_name="artifact_uploads")
    op.drop_column("artifact_uploads", "request_id")
