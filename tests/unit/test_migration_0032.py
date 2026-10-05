"""Migration 0032 on SQLite, and on Postgres when APIPI_TEST_DATABASE_URL is set.

The Postgres run creates and drops its own database next to the test
database, so it never touches the shared schema.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
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
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_session_files = sa.table(
    "session_files",
    sa.column("tenant_id", sa.Uuid()),
    sa.column("session_id", sa.Uuid()),
    sa.column("file_id", sa.String()),
    sa.column("path", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def _run(url: str, *statements: Any) -> None:
    async def go() -> None:
        engine = create_async_engine(store_url(url))
        try:
            async with engine.begin() as connection:
                for statement in statements:
                    await connection.execute(statement)
        finally:
            await engine.dispose()

    asyncio.run(go())


def _bind(ids: dict[str, Any], file_id: str, path: str | None) -> Any:
    return sa.insert(_session_files).values(
        tenant_id=ids["tenant"],
        session_id=ids["session"],
        file_id=file_id,
        path=path,
        created_at=_NOW,
    )


def _seed(url: str) -> dict[str, Any]:
    ids: dict[str, Any] = {
        "tenant": uuid.uuid4(),
        "session": uuid.uuid4(),
        "files": [f"file-{uuid.uuid4().hex}" for _ in range(4)],
    }
    _run(
        url,
        sa.insert(_tenants).values(id=ids["tenant"], name="t", created_at=_NOW),
        sa.insert(_sessions).values(
            id=ids["session"],
            tenant_id=ids["tenant"],
            status="idle",
            environment={},
            metadata={},
            required_actions=[],
            key_id="",
            vault_ids=[],
            created_at=_NOW,
            updated_at=_NOW,
        ),
        *[
            sa.insert(_files).values(
                id=file_id,
                tenant_id=ids["tenant"],
                filename="a.txt",
                purpose="user_data",
                size=1,
                created_at=_NOW,
            )
            for file_id in ids["files"]
        ],
        _bind(ids, ids["files"][0], None),
        _bind(ids, ids["files"][1], None),
    )
    return ids


def _migrate(url: str) -> None:
    command.upgrade(alembic_config(url), "0031_file_kinds")
    ids = _seed(url)
    files = ids["files"]
    upgrade_head(url)
    _run(url, _bind(ids, files[2], "attachments/a.txt"))
    with pytest.raises(IntegrityError):
        _run(url, _bind(ids, files[3], "attachments/a.txt"))
    command.downgrade(alembic_config(url), "0031_file_kinds")
    _run(url, _bind(ids, files[3], "attachments/a.txt"))
    with pytest.raises(IntegrityError):
        upgrade_head(url)


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


def test_session_file_paths_migration_on_sqlite(tmp_path: Path) -> None:
    _migrate(f"sqlite:///{tmp_path / 'apipi.db'}")


@pytest.mark.slow
@pytest.mark.skipif(not PG_URL, reason="needs APIPI_TEST_DATABASE_URL")
def test_session_file_paths_migration_on_postgres(postgres_url: str) -> None:
    _migrate(postgres_url)
