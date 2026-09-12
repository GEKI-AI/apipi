import json
import uuid
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import select

from apipi.api.sessions import _event_stream
from apipi.runtime import PUBLIC_EVENT_TYPES, EventHub
from apipi.store.engine import Store
from apipi.store.models import SessionRow
from apipi.tenants import provision_tenant


async def _token(store: Store, name: str = "t") -> str:
    async with store.session() as db:
        _tenant, token = await provision_tenant(db, name=name)
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _parse_sse(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        data = None
        for line in block.split("\n"):
            if line.startswith("data: "):
                data = line[6:]
        if data is not None:
            parsed = json.loads(data)
            assert isinstance(parsed, dict)
            events.append(parsed)
    return events


async def _read_stream_until_idle(
    store: Store,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    after_seq: int | None = None,
) -> str:
    agen = _event_stream(store, hub, tenant_id, session_id, after_seq)
    chunks: list[str] = []
    try:
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            chunks.append(chunk)
            types = [event["type"] for event in _parse_sse("".join(chunks))]
            if types and types[-1] == "agent.session.idle":
                return "".join(chunks)
    finally:
        await agen.aclose()
    return "".join(chunks)


async def _create_agent(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert response.status_code == 200
    return str(response.json()["id"])


async def test_session_crud_environment_none(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "metadata": {"k": "v"},
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "idle"
    assert body["environment"] == {"type": "none"}
    assert body["agent_id"] == agent_id
    assert body["metadata"] == {"k": "v"}
    assert body["required_actions"] == []
    session_id = body["id"]

    listed = await client.get("/v1/agents/sessions", headers=_auth(token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [session_id]

    got = await client.get(f"/v1/agents/sessions/{session_id}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["id"] == session_id

    updated = await client.post(
        f"/v1/agents/sessions/{session_id}",
        headers=_auth(token),
        json={"metadata": {"k": "2"}},
    )
    assert updated.status_code == 200
    assert updated.json()["metadata"] == {"k": "2"}

    deleted = await client.delete(
        f"/v1/agents/sessions/{session_id}", headers=_auth(token)
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"id": session_id, "deleted": True}
    gone = await client.get(f"/v1/agents/sessions/{session_id}", headers=_auth(token))
    assert gone.status_code == 404


async def test_inline_agent_is_not_saved(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent": {"name": "inline", "model": "test"},
            "environment": {"type": "none"},
        },
    )
    assert created.status_code == 200
    assert created.json()["agent_id"] is None
    agents = await client.get("/v1/agents", headers=_auth(token))
    assert agents.json() == {"data": []}


async def test_unimplemented_environment(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    agent_id = await _create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "not_implemented"
    assert response.json()["error"]["code"] == "self_hosted"


async def test_default_environment_is_openai_hosted(
    store: Store, client: AsyncClient
) -> None:
    token = await _token(store)
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id},
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["type"] == "openai_hosted"
    assert Path(env["directory"]).is_dir()


async def test_fake_harness_determined_events(
    store: Store, client: AsyncClient
) -> None:
    token = await _token(store)
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert events.status_code == 200
    types = [event["type"] for event in events.json()["data"]]
    assert types[0] == "agent.session.created"
    assert types[-1] == "agent.session.idle"
    assert set(types) <= PUBLIC_EVENT_TYPES
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello"

    posted = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert posted.status_code == 200
    assert posted.json()["status"] == "idle"
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["hello", "again"]


async def test_sse_replays_persisted_events(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "hi",
        },
    )
    session_id = created.json()["id"]
    stored = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    stored_types = [event["type"] for event in stored.json()["data"]]
    assert stored_types[0] == "agent.session.created"

    async with store.session() as db:
        row = await db.scalar(select(SessionRow))
        assert row is not None
        tenant_id = row.tenant_id
        sid = row.id
    streamed = _parse_sse(
        await _read_stream_until_idle(store, EventHub(), tenant_id, sid)
    )
    assert [event["type"] for event in streamed] == stored_types

    last_seq = stored.json()["data"][-1]["seq"]
    replay = _parse_sse(
        await _read_stream_until_idle(
            store, EventHub(), tenant_id, sid, after_seq=last_seq - 1
        )
    )
    assert replay[0]["seq"] == last_seq
    assert replay[0]["type"] == "agent.session.idle"


async def test_cross_tenant_session_is_404(store: Store, client: AsyncClient) -> None:
    token_a = await _token(store, "a")
    token_b = await _token(store, "b")
    agent_id = await _create_agent(client, token_a)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token_a),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    session_id = created.json()["id"]
    listed = await client.get("/v1/agents/sessions", headers=_auth(token_b))
    assert listed.json() == {"data": []}
    got = await client.get(f"/v1/agents/sessions/{session_id}", headers=_auth(token_b))
    assert got.status_code == 404
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token_b)
    )
    assert events.status_code == 404


async def test_unknown_session_field(store: Store, client: AsyncClient) -> None:
    token = await _token(store)
    agent_id = await _create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "foo": 1,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_field"
