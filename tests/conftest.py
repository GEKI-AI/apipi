import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
        sessions_dir=str(tmp_path / "sessions"),
    )


@pytest.fixture
async def client(settings: Settings, store: Store) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
