from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_invalid_thinking_metadata_is_400(client: AsyncClient) -> None:
    token = "pi-thinking-bad"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "metadata": {"apipi.thinking": "ultra"}},
    )
    assert created.status_code == 400
    assert "thinking" in created.json()["error"]["message"]


async def test_reasoning_effort_is_stored_as_thinking(client: AsyncClient) -> None:
    token = "pi-reasoning"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "reasoning": {"effort": "high"}},
    )
    assert created.status_code == 200
    assert created.json()["metadata"]["apipi.thinking"] == "high"
    assert created.json()["reasoning"]["effort"] == "high"
    summary = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "reasoning": {"summary": "auto"}},
    )
    assert summary.status_code == 400
    assert summary.json()["error"]["type"] == "not_implemented"


async def test_session_thinking_overrides_agent(client: AsyncClient) -> None:
    token = "pi-thinking-ok"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "metadata": {"apipi.thinking": "low"}},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "metadata": {"apipi.thinking": "high"},
        },
    )
    assert created.status_code == 200
    assert created.json()["metadata"]["apipi.thinking"] == "high"


async def test_metadata_update_replaces_title(client: AsyncClient) -> None:
    token = "pi-title-replace"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "metadata": {"apipi.title": "old", "apipi.title_status": "done", "keep": 1},
        },
    )
    assert created.status_code == 200
    updated = await client.post(
        f"/v1/agents/sessions/{created.json()['id']}",
        headers=_auth(token),
        json={"metadata": {"keep": 1}},
    )
    assert updated.status_code == 200
    metadata = updated.json()["metadata"]
    assert metadata == {"keep": 1}
    assert "apipi.title" not in metadata
    assert "apipi.title_status" not in metadata
