import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from apipi.store.engine import Store
from apipi.store.models import Base


def _sqlite_engine() -> AsyncEngine:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    url = os.environ.get("APIPI_TEST_DATABASE_URL")
    if url:
        engine = create_async_engine(url, pool_pre_ping=True)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE tenants CASCADE"))
    else:
        engine = _sqlite_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    result = Store(engine)
    yield result
    await result.dispose()


@pytest.fixture
async def db(store: Store) -> AsyncIterator[AsyncSession]:
    async with store.session() as session:
        yield session
