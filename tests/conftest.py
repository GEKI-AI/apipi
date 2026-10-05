import asyncio
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_timeout
from httpx import AsyncClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from apipi.config import Settings, store_url
from apipi.store.engine import Store
from apipi.store.models import Base
from apipi.worker.fake_harness import FakeHarness
from tests.support.migrations import SqliteRevisions


def _sqlite_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


@pytest.hookimpl
def pytest_exception_interact(node: pytest.Item | pytest.Collector) -> None:
    if not isinstance(node, pytest.Item) or node.config.getvalue("usepdb"):
        return
    if not node.config.pluginmanager.hasplugin("timeout"):
        return
    settings = pytest_timeout._get_item_settings(node)
    if settings.timeout and not settings.func_only:
        node.config.pluginmanager.hook.pytest_timeout_set_timer(
            item=node, settings=settings
        )


@pytest.fixture(scope="session")
def _sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sqlite") / "template.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture(scope="session")
def sqlite_revisions(tmp_path_factory: pytest.TempPathFactory) -> SqliteRevisions:
    return SqliteRevisions(tmp_path_factory.mktemp("revisions"))


@pytest.fixture
def postgres_url() -> Iterator[str]:
    url = os.environ.get("APIPI_TEST_DATABASE_URL")
    assert url is not None
    base = make_url(store_url(url))
    name = f"apipi_mig_{uuid.uuid4().hex[:12]}"

    async def admin(sql: str) -> None:
        engine = create_async_engine(base, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(sql))
        finally:
            await engine.dispose()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    url = os.environ.get("APIPI_TEST_DATABASE_URL")
    if url:
        engine = create_async_engine(url, pool_pre_ping=True)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE tenants, worker_tokens, workers, "
                    "worker_forwards CASCADE"
                )
            )
    else:
        path = tmp_path / "test.db"
        shutil.copyfile(request.getfixturevalue("_sqlite_template"), path)
        engine = _sqlite_engine(path)
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
def _late_first_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.worker.client.FIRST_INVENTORY_DELAY", 3600.0)


@pytest.fixture(autouse=True)
def _strict_protocol() -> Iterator[None]:
    from apipi.protocol import strict_parse

    with strict_parse():
        yield


@pytest.fixture(autouse=True)
def _wire_frames_match_schema() -> Iterator[None]:
    from tests.support import wire_schema

    wire_schema.start_capture()
    yield
    errors = wire_schema.capture_errors(wire_schema.stop_capture())
    if errors:
        pytest.fail(
            "frames do not match docs/worker-protocol/schema:\n"
            + "\n".join(errors[:10])
        )


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
def tool_harness() -> FakeHarness:
    harness = FakeHarness()
    harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    return harness


@pytest.fixture
async def client(
    settings: Settings, store: Store, worker_secret: str, worker_harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=worker_harness, token=worker_secret
    ) as (_app, client, _worker):
        yield client
