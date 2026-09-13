import uuid
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.auth import authenticate
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store
from apipi.store.turn_logs import get_turn_log
from apipi.tokens import hash_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_generates_request_id(client: AsyncClient) -> None:
    response = await client.get("/v1/agents", headers=_auth("t"))
    assert response.status_code == 200
    assert "x-apipi-instance" not in response.headers
    request_id = response.headers["x-request-id"]
    assert request_id
    assert request_id.isascii()
    assert len(request_id) <= 512
    other = await client.get("/v1/agents", headers=_auth("t"))
    assert other.headers["x-request-id"] != request_id


async def test_echoes_request_id(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/agents", headers={**_auth("t"), "x-request-id": "echo-me"}
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "echo-me"


async def test_honors_client_request_id(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/agents",
        headers={
            **_auth("t"),
            "x-request-id": "echo-me",
            "X-Client-Request-Id": "client-me",
        },
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "client-me"


async def test_turn_log_stores_request_id(client: AsyncClient, store: Store) -> None:
    token = "rid"
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers={**_auth(token), "X-Client-Request-Id": "turn-req"},
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.headers["x-request-id"] == "turn-req"
    turns = await client.get(
        f"/v1/agents/sessions/{created.json()['id']}/turns", headers=_auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        row = await get_turn_log(db, tenant_id, turn_id)
    assert row is not None
    assert row.request_id == "turn-req"


async def test_auth_context_headers(client: AsyncClient) -> None:
    token = "ctx"
    identity = authenticate(token)
    response = await client.get("/v1/agents", headers=_auth(token))
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
            **_auth("t"),
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
        headers={**_auth(token), "x-tenant-id": "00000000-0000-0000-0000-000000000000"},
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
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/agents", headers=_auth("t"))
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


async def test_error_echoes_request_id(client: AsyncClient) -> None:
    response = await client.get("/v1/agents", headers={"x-request-id": "err-1"})
    assert response.status_code == 401
    assert response.headers["x-request-id"] == "err-1"
