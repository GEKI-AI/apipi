from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent(client: AsyncClient, token: str, **extra: object) -> str:
    payload: dict[str, object] = {"name": "bot", "model": "test"}
    payload.update(extra)
    created = await client.post("/v1/agents", headers=_auth(token), json=payload)
    assert created.status_code == 200
    return str(created.json()["id"])


async def test_default_size_persisted(client: AsyncClient) -> None:
    token = "size-default"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    assert created.json()["environment"]["sandbox_size"] == "S"


async def test_environment_sandbox_size(client: AsyncClient) -> None:
    token = "size-env"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "M"},
        },
    )
    assert created.status_code == 200
    assert created.json()["environment"]["sandbox_size"] == "M"


async def test_session_metadata_sandbox_size(client: AsyncClient) -> None:
    token = "size-meta"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "metadata": {"apipi.sandbox_size": "L"},
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"]["sandbox_size"] == "L"
    assert body["metadata"]["apipi.sandbox_size"] == "L"


async def test_agent_metadata_sandbox_size(client: AsyncClient) -> None:
    token = "size-agent"
    agent_id = await _agent(
        client, token, metadata={"apipi.sandbox_size": "M", "keep": "me"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    assert created.json()["environment"]["sandbox_size"] == "M"


async def test_environment_overrides_metadata(client: AsyncClient) -> None:
    token = "size-override"
    agent_id = await _agent(client, token, metadata={"apipi.sandbox_size": "S"})
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "L"},
            "metadata": {"apipi.sandbox_size": "M"},
        },
    )
    assert created.status_code == 200
    assert created.json()["environment"]["sandbox_size"] == "L"


async def test_invalid_sandbox_size(client: AsyncClient) -> None:
    token = "size-bad"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "XL"},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"invalid_request", "validation_error"}


async def test_top_level_sandbox_size_unknown_field(client: AsyncClient) -> None:
    token = "size-top"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "sandbox_size": "M",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_field"


async def test_metadata_update_does_not_change_size(client: AsyncClient) -> None:
    token = "size-patch"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "sandbox_size": "M"},
        },
    )
    session_id = created.json()["id"]
    updated = await client.post(
        f"/v1/agents/sessions/{session_id}",
        headers=_auth(token),
        json={"metadata": {"apipi.sandbox_size": "L"}},
    )
    assert updated.status_code == 200
    assert updated.json()["environment"]["sandbox_size"] == "M"
    assert updated.json()["metadata"]["apipi.sandbox_size"] == "L"


async def test_gateway_default_size(settings: Settings, store: Store) -> None:
    sized = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        sandbox_default_size="L",
    )
    app = create_app(sized, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _agent(client, "size-gw")
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("size-gw"),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        assert created.status_code == 200
        assert created.json()["environment"]["sandbox_size"] == "L"


async def test_invalid_metadata_size(client: AsyncClient) -> None:
    token = "size-meta-bad"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "metadata": {"apipi.sandbox_size": "XL"},
        },
    )
    assert response.status_code == 400
    assert "sandbox_size" in response.json()["error"]["message"]
