from httpx import ASGITransport, AsyncClient
from tests.support.http import auth, create_agent

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store


async def test_default_size_persisted(client: AsyncClient) -> None:
    token = "size-default"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    assert created.json()["environment"]["sandbox_size"] == "S"


async def test_environment_sandbox_size(client: AsyncClient) -> None:
    token = "size-env"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "M"},
        },
    )
    assert created.status_code == 200
    assert created.json()["environment"]["sandbox_size"] == "M"


async def test_session_metadata_sandbox_size_is_rejected(client: AsyncClient) -> None:
    token = "size-meta-removed"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "metadata": {"apipi.sandbox_size": "L"},
        },
    )
    assert created.status_code == 400
    assert "container_size" in created.json()["error"]["message"]


async def test_agent_metadata_sandbox_size_is_rejected(client: AsyncClient) -> None:
    token = "size-agent"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.sandbox_size": "M", "keep": "me"},
        },
    )
    assert created.status_code == 400
    assert "container_size" in created.json()["error"]["message"]
    agent_id = await create_agent(client, token)
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=auth(token),
        json={"metadata": {"apipi.sandbox_size": "M"}},
    )
    assert updated.status_code == 400
    assert "container_size" in updated.json()["error"]["message"]


async def test_removed_size_key_fails_with_env_size(client: AsyncClient) -> None:
    token = "size-override"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "L"},
            "metadata": {"apipi.sandbox_size": "M"},
        },
    )
    assert created.status_code == 400
    assert "container_size" in created.json()["error"]["message"]


async def test_invalid_sandbox_size(client: AsyncClient) -> None:
    token = "size-bad"
    agent_id = await create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "XL"},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"invalid_request", "validation_error"}


async def test_top_level_sandbox_size_unknown_field(client: AsyncClient) -> None:
    token = "size-top"
    agent_id = await create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "sandbox_size": "M",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_field"


async def test_metadata_update_with_removed_key_fails(client: AsyncClient) -> None:
    token = "size-patch"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "M"},
        },
    )
    session_id = created.json()["id"]
    updated = await client.post(
        f"/v1/agents/sessions/{session_id}",
        headers=auth(token),
        json={"metadata": {"apipi.sandbox_size": "L"}},
    )
    assert updated.status_code == 400
    assert "container_size" in updated.json()["error"]["message"]


async def test_gateway_default_size(settings: Settings, store: Store) -> None:
    sized = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        sandbox_default_size="L",
    )
    from tests.support.split_worker import api_settings_for

    app = create_app(api_settings_for(sized), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await create_agent(client, "size-gw")
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth("size-gw"),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        assert created.status_code == 200
        assert created.json()["environment"]["sandbox_size"] == "L"
