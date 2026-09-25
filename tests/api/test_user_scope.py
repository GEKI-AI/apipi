from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import AuthRequest, tenant_from_key
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str, user: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if user is not None:
        headers["X-End-User"] = user
    return headers


async def _agent(client: AsyncClient, token: str, user: str | None) -> str:
    created = await client.post(
        "/v1/agents",
        headers=_auth(token, user),
        json={"name": "bot", "model": "test"},
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def test_same_bearer_different_users_are_separate(
    settings: Settings, store: Store
) -> None:
    class OrgAuth:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def __call__(self, token: str, request: AuthRequest) -> dict[str, object]:
            user = request.headers.get("x-end-user")
            self.calls.append(user)
            return {
                "key_id": hash_token(token),
                "tenant_id": tenant_from_key("shared-org"),
                "user_id": user,
                "cache_key": f"{token}:{user}",
            }

        def cache_key(self, token: str, request: AuthRequest) -> str:
            user = request.headers.get("x-end-user")
            return f"{token}:{user}"

    authenticate = OrgAuth()
    app = create_app(
        settings, store=store, harness=FakeHarness(), authenticate=authenticate
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _agent(client, "org", "ada")
        ada = await client.post(
            "/v1/agents/sessions",
            headers=_auth("org", "ada"),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        assert ada.status_code == 200
        assert ada.json()["user_id"] == "ada"
        before = len(authenticate.calls)
        again = await client.get("/v1/agents", headers=_auth("org", "ada"))
        assert again.status_code == 200
        assert len(authenticate.calls) == before
        bea = await client.post(
            "/v1/agents/sessions",
            headers=_auth("org", "bea"),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        assert bea.status_code == 200
        listed = await client.get("/v1/agents/sessions", headers=_auth("org", "ada"))
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["data"]] == [ada.json()["id"]]
        hidden = await client.get(
            f"/v1/agents/sessions/{ada.json()['id']}",
            headers=_auth("org", "bea"),
        )
        assert hidden.status_code == 404
        removed = await client.delete(
            f"/v1/agents/sessions/{ada.json()['id']}",
            headers=_auth("org", "bea"),
        )
        assert removed.status_code == 404
        resumed = await client.post(
            f"/v1/agents/sessions/{ada.json()['id']}/events",
            headers=_auth("org", "bea"),
            json={"type": "agent.session.input.message", "text": "hi"},
        )
        assert resumed.status_code == 404


async def test_missing_user_id_stays_tenant_scoped(
    settings: Settings, store: Store
) -> None:
    def authenticate(token: str) -> dict[str, object]:
        return {
            "key_id": hash_token(token),
            "tenant_id": tenant_from_key("shared-org"),
        }

    app = create_app(
        settings, store=store, harness=FakeHarness(), authenticate=authenticate
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _agent(client, "left", None)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("left"),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        assert created.status_code == 200
        assert created.json()["user_id"] is None
        listed = await client.get("/v1/agents/sessions", headers=_auth("right"))
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["data"]] == [created.json()["id"]]
