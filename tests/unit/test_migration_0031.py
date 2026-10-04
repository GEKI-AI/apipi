"""Migration 0031 on SQLite, and on Postgres when APIPI_TEST_DATABASE_URL is set.

The Postgres run creates and drops its own database next to the test
database, so it never touches the shared schema.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from apipi.config import store_url
from apipi.store.migrate import alembic_config, upgrade_head

PG_URL = os.environ.get("APIPI_TEST_DATABASE_URL")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_tenants = sa.table(
    "tenants",
    sa.column("id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_sessions = sa.table(
    "sessions",
    sa.column("id", sa.Uuid()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("status", sa.String()),
    sa.column("environment", sa.JSON()),
    sa.column("metadata", sa.JSON()),
    sa.column("required_actions", sa.JSON()),
    sa.column("key_id", sa.String()),
    sa.column("vault_ids", sa.JSON()),
    sa.column("user_id", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
_files = sa.table(
    "files",
    sa.column("id", sa.String()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("filename", sa.String()),
    sa.column("purpose", sa.String()),
    sa.column("size", sa.Integer()),
    sa.column("content_type", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_kinds = sa.table(
    "files",
    sa.column("id", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("user_id", sa.String()),
)
_purposes = sa.table(
    "files",
    sa.column("id", sa.String()),
    sa.column("purpose", sa.String()),
)
_artifact_uploads = sa.table(
    "artifact_uploads",
    sa.column("id", sa.Uuid()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("session_id", sa.Uuid()),
    sa.column("artifact_id", sa.Uuid()),
    sa.column("kind", sa.String()),
    sa.column("filename", sa.String()),
    sa.column("content_type", sa.String()),
    sa.column("declared_bytes", sa.Integer()),
    sa.column("status", sa.String()),
    sa.column("expires_at", sa.DateTime(timezone=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_session_files = sa.table(
    "session_files",
    sa.column("tenant_id", sa.Uuid()),
    sa.column("session_id", sa.Uuid()),
    sa.column("file_id", sa.String()),
    sa.column("path", sa.String()),
    sa.column("item_id", sa.Uuid()),
)
_uploads = sa.table(
    "uploads",
    sa.column("id", sa.Uuid()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("purpose", sa.String()),
    sa.column("object_id", sa.String()),
    sa.column("filename", sa.String()),
    sa.column("content_type", sa.String()),
    sa.column("declared_bytes", sa.Integer()),
    sa.column("status", sa.String()),
    sa.column("expires_at", sa.DateTime(timezone=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def _run(url: str, *statements: Any) -> list[list[Any]]:
    async def go() -> list[list[Any]]:
        engine = create_async_engine(store_url(url))
        out: list[list[Any]] = []
        try:
            async with engine.begin() as connection:
                for statement in statements:
                    result = await connection.execute(statement)
                    out.append(list(result.all()) if result.returns_rows else [])
        finally:
            await engine.dispose()
        return out

    return asyncio.run(go())


def _file(tenant: uuid.UUID, file_id: str, name: str) -> Any:
    return sa.insert(_files).values(
        id=file_id,
        tenant_id=tenant,
        filename=name,
        purpose="user_data",
        size=4,
        content_type="image/png",
        created_at=_NOW,
    )


def _image_upload(
    tenant: uuid.UUID, session: uuid.UUID, artifact: uuid.UUID, status: str
) -> Any:
    return sa.insert(_artifact_uploads).values(
        id=uuid.uuid4(),
        tenant_id=tenant,
        session_id=session,
        artifact_id=artifact,
        kind="input_image",
        filename="image",
        content_type="image/png",
        declared_bytes=4,
        status=status,
        expires_at=_NOW + timedelta(minutes=15),
        created_at=_NOW,
    )


def _seed(url: str) -> dict[str, Any]:
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    gone_session = uuid.uuid4()
    image = uuid.uuid4()
    orphan = uuid.uuid4()
    pending = uuid.uuid4()
    ids = {
        "tenant": tenant,
        "session": session,
        "image": f"file-{image.hex}",
        "orphan": f"file-{orphan.hex}",
        "plain": f"file-{uuid.uuid4().hex}",
    }
    _run(
        url,
        sa.insert(_tenants).values(id=tenant, name="t", created_at=_NOW),
        sa.insert(_sessions).values(
            id=session,
            tenant_id=tenant,
            status="idle",
            environment={},
            metadata={},
            required_actions=[],
            key_id="",
            vault_ids=[],
            user_id="ada",
            created_at=_NOW,
            updated_at=_NOW,
        ),
        _file(tenant, ids["image"], "image"),
        _file(tenant, ids["orphan"], "image"),
        _file(tenant, ids["plain"], "photo.png"),
        _image_upload(tenant, session, image, "complete"),
        _image_upload(tenant, session, image, "complete"),
        _image_upload(tenant, gone_session, orphan, "complete"),
        _image_upload(tenant, session, pending, "pending"),
    )
    return ids


def _check_upgraded(url: str, ids: dict[str, Any]) -> None:
    kinds, bound = _run(
        url,
        sa.select(_kinds.c.id, _kinds.c.kind, _kinds.c.user_id),
        sa.select(
            _session_files.c.tenant_id,
            _session_files.c.session_id,
            _session_files.c.file_id,
            _session_files.c.path,
            _session_files.c.item_id,
        ),
    )
    assert {row[0]: (row[1], row[2]) for row in kinds} == {
        ids["image"]: ("image", "ada"),
        ids["orphan"]: ("image", None),
        ids["plain"]: ("file", None),
    }
    assert [tuple(row) for row in bound] == [
        (ids["tenant"], ids["session"], ids["image"], None, None)
    ]
    with pytest.raises(IntegrityError):
        _run(
            url,
            sa.update(_kinds).where(_kinds.c.id == ids["plain"]).values(kind="bogus"),
        )
    _run(
        url,
        *[
            sa.insert(_uploads).values(
                id=uuid.uuid4(),
                tenant_id=ids["tenant"],
                purpose=purpose,
                object_id=f"file-{uuid.uuid4().hex}",
                filename="notes.txt",
                content_type="text/plain",
                declared_bytes=5,
                status="pending",
                expires_at=_NOW,
                created_at=_NOW,
            )
            for purpose in ("attachment", "image")
        ],
        sa.update(_purposes)
        .where(_purposes.c.id == ids["image"])
        .values(purpose="vision"),
    )


def _check_downgraded(url: str, ids: dict[str, Any]) -> None:
    purposes, files = _run(
        url,
        sa.select(_uploads.c.purpose),
        sa.select(_purposes.c.purpose).where(_purposes.c.id == ids["image"]),
    )
    assert [row[0] for row in purposes] == ["file", "file"]
    assert [row[0] for row in files] == ["user_data"]
    with pytest.raises(Exception, match="session_files"):
        _run(url, sa.select(_session_files.c.file_id))
    with pytest.raises(Exception, match="kind"):
        _run(url, sa.select(_kinds.c.kind))


def _migrate(url: str) -> None:
    command.upgrade(alembic_config(url), "0030_worker_forwards")
    ids = _seed(url)
    upgrade_head(url)
    _check_upgraded(url, ids)
    command.downgrade(alembic_config(url), "0030_worker_forwards")
    _check_downgraded(url, ids)
    upgrade_head(url)
    _check_upgraded(url, ids)


@pytest.fixture
def postgres_url() -> Iterator[str]:
    assert PG_URL is not None
    base = make_url(store_url(PG_URL))
    name = f"apipi_mig_{uuid.uuid4().hex[:12]}"

    async def admin(sql: str) -> None:
        engine = create_async_engine(base, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(sa.text(sql))
        finally:
            await engine.dispose()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_file_kinds_migration_on_sqlite(tmp_path: Path) -> None:
    _migrate(f"sqlite:///{tmp_path / 'apipi.db'}")


@pytest.mark.slow
@pytest.mark.skipif(not PG_URL, reason="needs APIPI_TEST_DATABASE_URL")
def test_file_kinds_migration_on_postgres(postgres_url: str) -> None:
    _migrate(postgres_url)
