import uuid

from httpx import AsyncClient

from apipi.store.engine import Store
from apipi.tenants import provision_tenant


async def _token(store: Store, name: str = "t") -> str:
    async with store.session() as db:
        _tenant, token = await provision_tenant(db, name=name)
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_fresh_tenant_has_no_agents(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    response = await client.get("/v1/agents", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {"data": []}


async def test_agent_crud(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "one",
            "model": "test-model",
            "instructions": "be brief",
            "metadata": {"k": "v"},
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "echo",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "type": "mcp",
                    "server_label": "tavily",
                    "server_url": "https://mcp.tavily.com/mcp",
                    "headers": {"Authorization": "Bearer x"},
                },
                {
                    "type": "mcp",
                    "server_label": "playwright",
                    "command": "npx",
                    "args": ["-y", "@playwright/mcp@latest"],
                },
            ],
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert set(body) == {
        "id",
        "name",
        "model",
        "instructions",
        "metadata",
        "tools",
        "created_at",
        "updated_at",
    }
    assert body["name"] == "one"
    assert body["model"] == "test-model"
    assert body["instructions"] == "be brief"
    assert body["metadata"] == {"k": "v"}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][1]["server_url"] == "https://mcp.tavily.com/mcp"
    assert body["tools"][2]["command"] == "npx"
    agent_id = body["id"]

    listed = await client.get("/v1/agents", headers=_auth(token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [agent_id]

    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["name"] == "one"

    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"name": "two"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "two"
    assert updated.json()["model"] == "test-model"

    missing = await client.get(f"/v1/agents/{uuid.uuid4()}", headers=_auth(token))
    assert missing.status_code == 404

    deleted = await client.delete(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert deleted.status_code == 200
    assert deleted.json() == {"id": agent_id, "deleted": True}

    gone = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert gone.status_code == 404


async def test_unknown_field_is_rejected(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    response = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "one", "vaults": []},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_field"
    listed = await client.get("/v1/agents", headers=_auth(token))
    assert listed.json() == {"data": []}


async def test_unimplemented_agent_fields(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    for field in ("multi_agent", "tool_search", "programmatic_tool_calling"):
        response = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "one", field: True},
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["type"] == "not_implemented"
        assert error["code"] == field


async def test_cross_tenant_agent_is_404(store: Store, client: AsyncClient) -> None:
    token_a = await _token(store, "a")
    token_b = await _token(store, "b")
    created = await client.post(
        "/v1/agents", headers=_auth(token_a), json={"name": "secret"}
    )
    agent_id = created.json()["id"]

    listed = await client.get("/v1/agents", headers=_auth(token_b))
    assert listed.json() == {"data": []}

    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token_b))
    assert got.status_code == 404

    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token_b),
        json={"name": "stolen"},
    )
    assert updated.status_code == 404

    deleted = await client.delete(f"/v1/agents/{agent_id}", headers=_auth(token_b))
    assert deleted.status_code == 404

    still = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token_a))
    assert still.status_code == 200
    assert still.json()["name"] == "secret"
