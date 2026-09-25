from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent(client: AsyncClient, token: str, **extra: object) -> str:
    payload: dict[str, object] = {"name": "bot", "model": "test"}
    payload.update(extra)
    created = await client.post("/v1/agents", headers=_auth(token), json=payload)
    assert created.status_code == 200, created.text
    return str(created.json()["id"])


async def test_agent_rejects_bad_sandbox_size(client: AsyncClient) -> None:
    token = "agent-size-bad"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "metadata": {"apipi.sandbox_size": "xl"}},
    )
    assert created.status_code == 400
    assert created.json()["error"]["code"] == "invalid_request"
    agent_id = await _agent(client, token)
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"metadata": {"apipi.sandbox_size": "xl"}},
    )
    assert updated.status_code == 400
    assert "sandbox_size" in updated.json()["error"]["message"]
    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert got.status_code == 200
    assert "apipi.sandbox_size" not in got.json()["metadata"]


async def test_agent_rejects_unknown_sandbox_image(
    settings: Settings, store: Store
) -> None:
    limited = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        sandbox_images=["default", "browser"],
    )
    app = create_app(limited, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "agent-image-bad"
        created = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "metadata": {"apipi.sandbox_image": "notreal"}},
        )
        assert created.status_code == 400
        assert "sandbox_image" in created.json()["error"]["message"]
        agent_id = await _agent(client, token)
        updated = await client.post(
            f"/v1/agents/{agent_id}",
            headers=_auth(token),
            json={"metadata": {"apipi.sandbox_image": "notreal"}},
        )
        assert updated.status_code == 400


async def test_agent_rejects_browser_below_min_size(client: AsyncClient) -> None:
    token = "agent-browser-s"
    body = {"metadata": {"apipi.sandbox_image": "browser", "apipi.sandbox_size": "S"}}
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", **body},
    )
    assert created.status_code == 400
    assert "needs sandbox_size" in created.json()["error"]["message"]
    agent_id = await _agent(client, token)
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json=body,
    )
    assert updated.status_code == 400


async def test_agent_sandbox_pair_is_inherited(client: AsyncClient) -> None:
    token = "agent-browser-m"
    agent_id = await _agent(
        client,
        token,
        metadata={"apipi.sandbox_image": "browser", "apipi.sandbox_size": "M"},
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    environment = created.json()["environment"]
    assert environment["sandbox_size"] == "M"
    assert environment["sandbox_image"] == "browser"


async def test_agent_without_sandbox_metadata(client: AsyncClient) -> None:
    token = "agent-sandbox-omit"
    agent_id = await _agent(client, token, metadata={"keep": "me"})
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"name": "renamed"},
    )
    assert updated.status_code == 200
    assert updated.json()["metadata"] == {"keep": "me"}
