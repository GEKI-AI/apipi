"""File kinds, file owners, and session file bindings (#513).

Revision ID: 0031_file_kinds
Revises: 0030_worker_forwards
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0031_file_kinds"
down_revision: str | None = "0030_worker_forwards"
branch_labels: str | None = None
depends_on: str | None = None

_BATCH = 500

_files = sa.table(
    "files",
    sa.column("id", sa.String()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("kind", sa.String()),
    sa.column("user_id", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_sessions = sa.table(
    "sessions",
    sa.column("id", sa.Uuid()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("user_id", sa.String()),
)
_uploads = sa.table(
    "artifact_uploads",
    sa.column("tenant_id", sa.Uuid()),
    sa.column("session_id", sa.Uuid()),
    sa.column("artifact_id", sa.Uuid()),
    sa.column("kind", sa.String()),
    sa.column("status", sa.String()),
)
_session_files = sa.table(
    "session_files",
    sa.column("tenant_id", sa.Uuid()),
    sa.column("session_id", sa.Uuid()),
    sa.column("file_id", sa.String()),
    sa.column("path", sa.String()),
    sa.column("item_id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def _backfill_images() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            _uploads.c.tenant_id, _uploads.c.session_id, _uploads.c.artifact_id
        ).where(_uploads.c.kind == "input_image", _uploads.c.status == "complete")
    ).all()
    seen: set[tuple[object, object, str]] = set()
    images: list[tuple[object, object, str]] = []
    for tenant_id, session_id, artifact_id in rows:
        key = (tenant_id, session_id, f"file-{artifact_id.hex}")
        if key not in seen:
            seen.add(key)
            images.append(key)
    for start in range(0, len(images), _BATCH):
        chunk = images[start : start + _BATCH]
        file_ids = sorted({file_id for _tenant, _session, file_id in chunk})
        files = {
            (tenant_id, file_id): created_at
            for file_id, tenant_id, created_at in bind.execute(
                sa.select(_files.c.id, _files.c.tenant_id, _files.c.created_at).where(
                    _files.c.id.in_(file_ids)
                )
            )
        }
        session_ids = sorted({session_id for _t, session_id, _f in chunk}, key=str)
        owners = {
            (tenant_id, session_id): user_id
            for session_id, tenant_id, user_id in bind.execute(
                sa.select(
                    _sessions.c.id, _sessions.c.tenant_id, _sessions.c.user_id
                ).where(_sessions.c.id.in_(session_ids))
            )
        }
        for tenant_id, session_id, file_id in chunk:
            created_at = files.get((tenant_id, file_id))
            if created_at is None:
                continue
            values: dict[str, object] = {"kind": "image"}
            if (tenant_id, session_id) in owners:
                values["user_id"] = owners[(tenant_id, session_id)]
                bind.execute(
                    sa.insert(_session_files).values(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        file_id=file_id,
                        path=None,
                        item_id=None,
                        created_at=created_at,
                    )
                )
            bind.execute(
                sa.update(_files)
                .where(_files.c.tenant_id == tenant_id, _files.c.id == file_id)
                .values(**values)
            )


def upgrade() -> None:
    with op.batch_alter_table("files") as batch:
        batch.add_column(
            sa.Column(
                "kind", sa.String(length=16), nullable=False, server_default="file"
            )
        )
        batch.add_column(sa.Column("user_id", sa.String(), nullable=True))
        batch.create_check_constraint(
            "files_kind_check", "kind IN ('file', 'attachment', 'image')"
        )
        batch.drop_constraint("files_purpose_check", type_="check")
        batch.create_check_constraint(
            "files_purpose_check", "purpose IN ('user_data', 'assistants', 'vision')"
        )
    op.create_index(
        "ix_files_tenant_kind_created", "files", ["tenant_id", "kind", "created_at"]
    )
    op.create_index("ix_files_kind_created", "files", ["kind", "created_at"])
    with op.batch_alter_table("uploads") as batch:
        batch.add_column(sa.Column("user_id", sa.String(), nullable=True))
        batch.drop_constraint("uploads_purpose_check", type_="check")
        batch.create_check_constraint(
            "uploads_purpose_check",
            "purpose IN ('file', 'skill', 'attachment', 'image')",
        )
    op.create_table(
        "session_files",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["sessions.tenant_id", "sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["files.tenant_id", "files.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "session_id", "file_id"),
    )
    op.create_index(
        "ix_session_files_tenant_file", "session_files", ["tenant_id", "file_id"]
    )
    _backfill_images()


def downgrade() -> None:
    op.drop_index("ix_session_files_tenant_file", table_name="session_files")
    op.drop_table("session_files")
    op.execute(
        "UPDATE uploads SET purpose = 'file' WHERE purpose IN ('attachment', 'image')"
    )
    op.execute("UPDATE files SET purpose = 'user_data' WHERE purpose = 'vision'")
    with op.batch_alter_table("uploads") as batch:
        batch.drop_constraint("uploads_purpose_check", type_="check")
        batch.create_check_constraint(
            "uploads_purpose_check", "purpose IN ('file', 'skill')"
        )
        batch.drop_column("user_id")
    op.drop_index("ix_files_kind_created", table_name="files")
    op.drop_index("ix_files_tenant_kind_created", table_name="files")
    with op.batch_alter_table("files") as batch:
        batch.drop_constraint("files_purpose_check", type_="check")
        batch.create_check_constraint(
            "files_purpose_check", "purpose IN ('user_data', 'assistants')"
        )
        batch.drop_constraint("files_kind_check", type_="check")
        batch.drop_column("user_id")
        batch.drop_column("kind")
