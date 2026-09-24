from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_agent_idle_ttl_round_trip(client: AsyncClient) -> None:
    token = "idle-agent"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "idle_ttl": "30m"},
    )
    assert created.status_code == 200
    assert created.json()["idle_ttl"] == "30m"
    agent_id = created.json()["id"]
    cleared = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"idle_ttl": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["idle_ttl"] is None


async def test_invalid_idle_ttl_is_400(client: AsyncClient) -> None:
    token = "idle-bad"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "idle_ttl": "soon"},
    )
    assert created.status_code == 400
    assert "idle_ttl" in created.json()["error"]["message"]


async def test_session_idle_ttl_stored(client: AsyncClient) -> None:
    token = "idle-session"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "idle_ttl": "1h"},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "idle_ttl": "30m",
        },
    )
    assert created.status_code == 200
    assert created.json()["idle_ttl"] == "30m"
