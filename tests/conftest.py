import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.store.models import Base
from apipi.worker.fake_harness import FakeHarness


def _sqlite_engine(path: Path | None = None) -> AsyncEngine:
    if path is None:
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if path is not None:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    url = os.environ.get("APIPI_TEST_DATABASE_URL")
    if url:
        engine = create_async_engine(url, pool_pre_ping=True)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE tenants, worker_tokens CASCADE"))
    else:
        engine = _sqlite_engine(tmp_path / "test.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    result = Store(engine)
    yield result
    await result.dispose()


@pytest.fixture
async def db(store: Store) -> AsyncIterator[AsyncSession]:
    async with store.session() as session:
        yield session


@pytest.fixture(autouse=True)
def _local_store_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIPI_LOCAL_STORE_DIR", str(tmp_path / "default-store"))


@pytest.fixture(autouse=True)
def _clear_model_list_cache() -> Iterator[None]:
    from apipi.common.models import clear_model_cache

    clear_model_cache()
    yield
    clear_model_cache()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        local_store_dir=str(tmp_path / "store"),
    )


@pytest.fixture
async def worker_secret(store: Store) -> AsyncIterator[str]:
    from apipi.services.worker_tokens import create_token

    created = await create_token(store, name="test-worker")
    yield created.secret


@pytest.fixture
def worker_harness() -> FakeHarness:
    return FakeHarness()


@pytest.fixture
async def client(
    settings: Settings, store: Store, worker_secret: str, worker_harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=worker_harness, token=worker_secret
    ) as (_app, client, _worker):
        yield client
