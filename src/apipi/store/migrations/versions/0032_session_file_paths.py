"""One workspace path per session file binding (#511).

Revision ID: 0032_session_file_paths
Revises: 0031_file_kinds
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0032_session_file_paths"
down_revision: str | None = "0031_file_kinds"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "uq_session_files_path",
        "session_files",
        ["tenant_id", "session_id", "path"],
        unique=True,
        postgresql_where=sa.text("path IS NOT NULL"),
        sqlite_where=sa.text("path IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_session_files_path", table_name="session_files")
