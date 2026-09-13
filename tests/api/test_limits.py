import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.auth import authenticate
from apipi.config import Settings
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


class _Alive:
    alive = True


@pytest.fixture
def limited_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
        sessions_dir=str(tmp_path / "sessions"),
        max_sessions=1,
        max_request_bytes=1024,
    )


@pytest.fixture
async def limited_client(
    limited_settings: Settings, store: Store
) -> AsyncIterator[AsyncClient]:
    pool = PiPool(limited_settings)
    pool._procs[uuid.uuid4()] = cast(PiProc, _Alive())
    app = create_app(limited_settings, store=store, harness=FakeHarness(), pool=pool)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_capacity_rejects_new_turn(limited_client: AsyncClient) -> None:
    token = {"Authorization": "Bearer t"}
    agent = await limited_client.post(
        "/v1/agents", headers=token, json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    created = await limited_client.post(
        "/v1/agents/sessions",
        headers=token,
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "Hello",
        },
    )
    assert created.status_code == 429
    error = created.json()["error"]
    assert error["code"] == "capacity"


async def test_capacity_rejects_tenant_over_cap(store: Store, tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
        sessions_dir=str(tmp_path / "sessions"),
        max_sessions=8,
        max_sessions_per_tenant=1,
    )
    pool = PiPool(settings)
    tenant_a = authenticate("a").tenant_id
    live = uuid.uuid4()
    pool._procs[live] = cast(PiProc, _Alive())
    pool._tenants[live] = tenant_a
    app = create_app(settings, store=store, harness=FakeHarness(), pool=pool)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        blocked = await client.post(
            "/v1/agents",
            headers={"Authorization": "Bearer a"},
            json={"name": "bot", "model": "test"},
        )
        assert blocked.status_code == 200
        created = await client.post(
            "/v1/agents/sessions",
            headers={"Authorization": "Bearer a"},
            json={
                "agent_id": blocked.json()["id"],
                "environment": {"type": "none"},
                "input": "Hello",
            },
        )
        assert created.status_code == 429
        assert created.json()["error"]["code"] == "capacity_tenant"
        other = await client.post(
            "/v1/agents",
            headers={"Authorization": "Bearer b"},
            json={"name": "bot", "model": "test"},
        )
        assert other.status_code == 200
        ok = await client.post(
            "/v1/agents/sessions",
            headers={"Authorization": "Bearer b"},
            json={
                "agent_id": other.json()["id"],
                "environment": {"type": "none"},
                "input": "Hello",
            },
        )
        assert ok.status_code == 200


async def test_payload_too_large(limited_settings: Settings, store: Store) -> None:
    app = create_app(limited_settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agents",
            headers={"Authorization": "Bearer t"},
            content=b"x" * 2048,
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
