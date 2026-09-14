import uuid

from httpx import AsyncClient

from apipi.runtime import PUBLIC_EVENT_TYPES


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


async def test_export_after_a_turn(client: AsyncClient) -> None:
    token = _token()
    session_id = await _session_with_turn(client, token)
    exported = await client.get(
        f"/v1/agents/sessions/{session_id}/export", headers=_auth(token)
    )
    assert exported.status_code == 200
    body = exported.json()
    types = [event["type"] for event in body["events"]]
    assert types[0] == "agent.session.created"
    assert types[-1] == "agent.session.idle"
    assert set(types) <= PUBLIC_EVENT_TYPES
    assert "agent.session.turn.output_text.delta" not in types
    done = [
        event
        for event in body["events"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello"
    assert len(body["turns"]) == 1
    assert body["turns"][0]["status"] == "completed"
    assert body["turns"][0]["session_id"] == session_id
    roles = [item["data"]["role"] for item in body["items"]]
    assert roles == ["user", "assistant"]
    assert body["items"][0]["data"]["content"] == "hello"
    assert body["items"][1]["data"]["content"] == "hello"
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth(token)
    )
    assert body["events"] == events.json()["data"]
    assert body["turns"] == turns.json()["data"]
    assert body["items"] == items.json()["data"]


async def test_export_unknown_session_is_404(client: AsyncClient) -> None:
    token = _token()
    missing = await client.get(
        f"/v1/agents/sessions/{uuid.uuid4()}/export", headers=_auth(token)
    )
    assert missing.status_code == 404


async def test_export_cross_tenant_is_404(client: AsyncClient) -> None:
    token_a = _token("a")
    token_b = _token("b")
    session_id = await _session_with_turn(client, token_a)
    other = await client.get(
        f"/v1/agents/sessions/{session_id}/export", headers=_auth(token_b)
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"
