import uuid

from httpx import AsyncClient
from sqlalchemy import select

from apipi.gateway.auth import authenticate
from apipi.store.engine import Store
from apipi.store.models import Tenant


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_missing_bearer_is_401(client: AsyncClient) -> None:
    response = await client.get(f"/v1/agents/{uuid.uuid4()}")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["type"] == "invalid_request"
    assert body["error"]["code"] == "unauthorized"


async def test_empty_bearer_is_401(client: AsyncClient) -> None:
    response = await client.get(
        f"/v1/agents/{uuid.uuid4()}",
        headers={"Authorization": "Bearer "},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_any_bearer_is_accepted(client: AsyncClient) -> None:
    response = await client.get("/v1/agents", headers=_auth("any-key"))
    assert response.status_code == 200
    assert response.json() == {"data": []}


async def test_same_bearer_is_stable(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/agents", headers=_auth("stable"), json={"name": "one"}
    )
    assert created.status_code == 200
    agent_id = created.json()["id"]
    again = await client.get(f"/v1/agents/{agent_id}", headers=_auth("stable"))
    assert again.status_code == 200
    assert again.json()["name"] == "one"


async def test_cross_tenant_id_is_404_not_403(client: AsyncClient) -> None:
    created = await client.post("/v1/agents", headers=_auth("a"), json={"name": "one"})
    assert created.status_code == 200
    agent_id = created.json()["id"]

    ok = await client.get(f"/v1/agents/{agent_id}", headers=_auth("a"))
    assert ok.status_code == 200
    assert ok.json()["name"] == "one"

    other = await client.get(f"/v1/agents/{agent_id}", headers=_auth("b"))
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"


async def test_tenant_row_created_on_first_use(
    store: Store, client: AsyncClient
) -> None:
    identity = authenticate("first-use")
    async with store.session() as db:
        assert (
            await db.scalar(select(Tenant).where(Tenant.id == identity.tenant_id))
            is None
        )
    listed = await client.get("/v1/agents", headers=_auth("first-use"))
    assert listed.status_code == 200
    async with store.session() as db:
        tenant = await db.scalar(select(Tenant).where(Tenant.id == identity.tenant_id))
        assert tenant is not None
        assert tenant.id == identity.tenant_id
