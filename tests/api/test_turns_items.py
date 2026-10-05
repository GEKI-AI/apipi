from httpx import AsyncClient

from apipi.worker.fake_harness import FAKE_USAGE


def _token(name: str = "t") -> str:
    return name


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _session_with_turn(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def test_turns_and_items_after_a_turn(client: AsyncClient) -> None:
    token = _token()
    session_id = await _session_with_turn(client, token)

    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    assert turns.status_code == 200
    data = turns.json()["data"]
    assert len(data) == 1
    assert data[0]["status"] == "completed"
    assert data[0]["session_id"] == session_id
    turn_id = data[0]["id"]

    one = await client.get(
        f"/v1/agents/sessions/{session_id}/turns/{turn_id}",
        headers=_auth(token),
    )
    assert one.status_code == 200
    assert one.json()["id"] == turn_id
    assert one.json()["usage"] == FAKE_USAGE

    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth(token)
    )
    assert items.status_code == 200
    types = [item["type"] for item in items.json()["data"]]
    assert types == ["message", "message"]
    roles = [item["data"]["role"] for item in items.json()["data"]]
    assert roles == ["user", "assistant"]
    assert items.json()["data"][0]["data"]["content"] == "hello"
    assert items.json()["data"][1]["data"]["content"] == "hello"
    assert items.json()["data"][0]["turn_id"] == turn_id


async def test_turn_from_other_session_is_404(client: AsyncClient) -> None:
    token = _token()
    session_a = await _session_with_turn(client, token)
    session_b = await _session_with_turn(client, token)
    turns_a = await client.get(
        f"/v1/agents/sessions/{session_a}/turns", headers=_auth(token)
    )
    turn_id = turns_a.json()["data"][0]["id"]
    other = await client.get(
        f"/v1/agents/sessions/{session_b}/turns/{turn_id}",
        headers=_auth(token),
    )
    assert other.status_code == 404
