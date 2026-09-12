import uuid

from httpx import AsyncClient

from apipi.runtime import FAKE_USAGE
from apipi.store.engine import Store
from apipi.store.repo import get_session_turn
from apipi.tokens import hash_token


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


async def test_completed_turn_persists_usage(client: AsyncClient, store: Store) -> None:
    token = _token()
    session_id = await _session_with_turn(client, token)
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    completed = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.completed"
    ]
    assert len(completed) == 1
    usage = completed[0]["data"]["usage"]
    assert usage == FAKE_USAGE
    assert set(usage) == set(FAKE_USAGE)
    assert "cost" not in usage
    assert "prompt" not in usage
    assert "hello" not in str(usage)
    turn_id = completed[0]["data"]["turn_id"]
    one = await client.get(
        f"/v1/agents/sessions/{session_id}/turns/{turn_id}",
        headers=_auth(token),
    )
    assert one.json()["usage"] == FAKE_USAGE
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        row = await get_session_turn(
            db, tenant_id, uuid.UUID(session_id), uuid.UUID(turn_id)
        )
    assert row is not None
    assert row.usage == FAKE_USAGE


async def test_turns_items_unknown_session_is_404(client: AsyncClient) -> None:
    token = _token()
    missing = uuid.uuid4()
    turns = await client.get(
        f"/v1/agents/sessions/{missing}/turns", headers=_auth(token)
    )
    assert turns.status_code == 404
    items = await client.get(
        f"/v1/agents/sessions/{missing}/items", headers=_auth(token)
    )
    assert items.status_code == 404


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


async def test_cross_tenant_turns_items_are_404(client: AsyncClient) -> None:
    token_a = _token("a")
    token_b = _token("b")
    session_id = await _session_with_turn(client, token_a)
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token_b)
    )
    assert turns.status_code == 404
    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth(token_b)
    )
    assert items.status_code == 404
