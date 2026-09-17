from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from apipi.auth import AuthIdentity
from apipi.config import Settings, extend_settings
from apipi.gateway import Gateway, create_app
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _only_ok(bearer: str) -> AuthIdentity | None:
    if bearer != "ok":
        return None
    return AuthIdentity(key_id="ok", tenant_id=uuid5(NAMESPACE_URL, "ok"))


@pytest.fixture
def instance_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        instance_id="node-a",
    )


async def test_verbose_gateway_pattern(settings: Settings, store: Store) -> None:
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        await gateway.startup()
        try:
            yield
        finally:
            await gateway.shutdown()

    app = FastAPI(lifespan=lifespan)
    gateway.configure(app)
    app.include_router(gateway.routers.sessions)
    app.include_router(gateway.routers.agents)
    app.include_router(gateway.routers.health)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        agents = await client.get("/v1/agents", headers=_auth("t"))
        assert agents.status_code == 200
        assert agents.json() == {"data": []}


async def test_authenticate_inject(settings: Settings, store: Store) -> None:
    gateway = Gateway.create(
        settings, store=store, harness=FakeHarness(), authenticate=_only_ok
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        await gateway.startup()
        try:
            yield
        finally:
            await gateway.shutdown()

    app = FastAPI(lifespan=lifespan)
    gateway.configure(app)
    app.include_router(gateway.routers.agents)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/v1/agents", headers=_auth("nope"))
        assert denied.status_code == 401
        allowed = await client.get("/v1/agents", headers=_auth("ok"))
        assert allowed.status_code == 200


async def test_injected_store_not_disposed(settings: Settings, store: Store) -> None:
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    with patch.object(store, "dispose", new_callable=AsyncMock) as mocked:
        await gateway.startup()
        await gateway.shutdown()
        mocked.assert_not_called()
    async with store.session() as db:
        await db.execute(text("SELECT 1"))


async def test_owned_store_disposed(tmp_path: Path) -> None:
    settings = extend_settings(
        database_url="sqlite+aiosqlite:///:memory:",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )
    gateway = Gateway.create(settings, harness=FakeHarness())
    with patch.object(gateway.store, "dispose", new_callable=AsyncMock) as mocked:
        await gateway.startup()
        await gateway.shutdown()
        mocked.assert_awaited_once()


async def test_prefix_skips_health_context(
    instance_settings: Settings, store: Store
) -> None:
    inner = create_app(instance_settings, store=store, harness=FakeHarness())
    app = FastAPI()
    app.mount("/apipi", inner)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/apipi/health")
        agents = await client.get("/apipi/v1/agents", headers=_auth("t"))
    assert health.status_code == 200
    assert "x-apipi-instance" not in health.headers
    assert "x-request-id" not in health.headers
    assert agents.status_code == 200
    assert agents.headers["x-apipi-instance"] == "node-a"
