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
