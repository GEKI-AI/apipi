import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.http import auth, tenant_of
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import authenticate
from apipi.store.engine import Store
from apipi.store.turn_logs import get_turn_log


async def test_generates_request_id(client: AsyncClient) -> None:
    response = await client.get("/v1/agents", headers=auth("t"))
    assert response.status_code == 200
    assert "x-apipi-instance" not in response.headers
    request_id = response.headers["x-request-id"]
    assert request_id
    assert request_id.isascii()
    assert len(request_id) <= 512
    other = await client.get("/v1/agents", headers=auth("t"))
    assert other.headers["x-request-id"] != request_id


@pytest.mark.parametrize(
    ("headers", "status", "request_id"),
    [
        ({**auth("t"), "x-request-id": "echo-me"}, 200, "echo-me"),
        (
            {
                **auth("t"),
                "x-request-id": "echo-me",
                "X-Client-Request-Id": "client-me",
            },
            200,
            "client-me",
        ),
        ({"x-request-id": "err-1"}, 401, "err-1"),
    ],
    ids=["request_id", "client_request_id", "error"],
)
async def test_echoes_request_id(
    client: AsyncClient, headers: dict[str, str], status: int, request_id: str
) -> None:
    response = await client.get("/v1/agents", headers=headers)
    assert response.status_code == status
    assert response.headers["x-request-id"] == request_id


async def test_turn_log_stores_request_id(client: AsyncClient, store: Store) -> None:
    token = "rid"
    agent = await client.post(
        "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers={**auth(token), "X-Client-Request-Id": "turn-req"},
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.headers["x-request-id"] == "turn-req"
    turns = await client.get(
        f"/v1/agents/sessions/{created.json()['id']}/turns", headers=auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    tenant_id = tenant_of(token)
    async with store.session() as db:
        row = await get_turn_log(db, tenant_id, turn_id)
    assert row is not None
    assert row.request_id == "turn-req"


async def test_auth_context_headers(client: AsyncClient) -> None:
    token = "ctx"
    identity = authenticate(token)
    response = await client.get("/v1/agents", headers=auth(token))
    assert response.status_code == 200
    assert response.headers["x-tenant-id"] == str(identity.tenant_id)
    assert response.headers["x-user-id"] == identity.key_id


async def test_unauthorized_has_no_tenant_headers(client: AsyncClient) -> None:
    response = await client.get("/v1/agents")
    assert response.status_code == 401
    assert "x-tenant-id" not in response.headers
    assert "x-user-id" not in response.headers


async def test_trace_id_from_traceparent(client: AsyncClient) -> None:
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    response = await client.get(
        "/v1/agents",
        headers={
            **auth("t"),
            "traceparent": f"00-{trace_id}-b7ad6b7169203331-01",
        },
    )
    assert response.status_code == 200
    assert response.headers["x-trace-id"] == trace_id


async def test_incoming_tenant_header_is_not_trusted(client: AsyncClient) -> None:
    token = "ctx-trust"
    identity = authenticate(token)
    response = await client.get(
        "/v1/agents",
        headers={**auth(token), "x-tenant-id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 200
    assert response.headers["x-tenant-id"] == str(identity.tenant_id)


async def test_instance_header_when_set(store: Store, tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        instance_id="node-a",
    )
    app = create_app(api_settings_for(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/agents", headers=auth("t"))
        health = await client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-apipi-instance"] == "node-a"
    assert health.status_code == 200
    assert "x-apipi-instance" not in health.headers
    assert "x-tenant-id" not in health.headers
    assert "x-user-id" not in health.headers


async def test_health_has_no_request_id(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert "x-request-id" not in response.headers
