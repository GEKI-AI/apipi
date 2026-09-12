import uuid

from httpx import AsyncClient

from apipi.store.engine import Store
from apipi.store.turn_logs import get_turn_log
from apipi.tokens import hash_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_generates_request_id(client: AsyncClient) -> None:
    response = await client.get("/v1/agents", headers=_auth("t"))
    assert response.status_code == 200
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


async def test_health_has_no_request_id(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert "x-request-id" not in response.headers


async def test_error_echoes_request_id(client: AsyncClient) -> None:
    response = await client.get("/v1/agents", headers={"x-request-id": "err-1"})
    assert response.status_code == 401
    assert response.headers["x-request-id"] == "err-1"
