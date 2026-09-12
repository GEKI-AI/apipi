import uuid

from httpx import AsyncClient

from apipi.store.engine import Store
from apipi.store.repo import create_agent, get_api_key_by_hash
from apipi.tenants import provision_tenant
from apipi.tokens import hash_token


async def test_missing_bearer_is_401(client: AsyncClient) -> None:
    response = await client.get(f"/v1/agents/{uuid.uuid4()}")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["type"] == "invalid_request"
    assert body["error"]["code"] == "unauthorized"


async def test_invalid_bearer_is_401(client: AsyncClient) -> None:
    response = await client.get(
        f"/v1/agents/{uuid.uuid4()}",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_cross_tenant_id_is_404_not_403(
    store: Store, client: AsyncClient
) -> None:
    async with store.session() as db:
        a, token_a = await provision_tenant(db, name="a")
        _b, token_b = await provision_tenant(db, name="b")
        agent = await create_agent(db, a.id, name="one")
        agent_id = agent.id

    ok = await client.get(
        f"/v1/agents/{agent_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert ok.status_code == 200
    assert ok.json()["name"] == "one"
    assert ok.json()["id"] == str(agent_id)

    other = await client.get(
        f"/v1/agents/{agent_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"


async def test_provision_stores_hash_not_token(store: Store) -> None:
    async with store.session() as db:
        _tenant, token = await provision_tenant(db, name="local")
        assert await get_api_key_by_hash(db, token) is None
        assert await get_api_key_by_hash(db, hash_token(token)) is not None
