from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from apipi.config import is_sqlite_url, store_url


def _ensure_sqlite_dir(url: str) -> None:
    if not is_sqlite_url(url) or ":memory:" in url:
        return
    raw = url.split("sqlite+aiosqlite:///", 1)[-1]
    if not raw:
        return
    path = Path(raw)
    if path.parent.as_posix() not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)


def create_engine(url: str, *, pool_size: int = 5) -> AsyncEngine:
    resolved = store_url(url)
    if is_sqlite_url(resolved):
        _ensure_sqlite_dir(resolved)
        engine = create_async_engine(
            resolved,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in resolved:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.fetchall()
            cursor.close()

        return engine
    return create_async_engine(resolved, pool_pre_ping=True, pool_size=pool_size)


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
