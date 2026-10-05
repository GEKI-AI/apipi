import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient

from apipi.config import Settings
from apipi.gateway.tokens import hash_token
from apipi.store.engine import Store
from apipi.store.turn_logs import get_turn_log
from apipi.worker.fake_harness import FakeHarness


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tool_harness() -> FakeHarness:
    harness = FakeHarness()
    harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    return harness


def _block_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("split worker must not construct storage clients")

    import apipi.store.blobs as blobs
    import apipi.store.engine as engine

    monkeypatch.setattr(engine, "create_engine", _boom)
    monkeypatch.setattr(engine, "Store", _boom)
    monkeypatch.setattr(blobs, "object_store", _boom)
    monkeypatch.setattr(blobs, "blob_store", _boom)
    monkeypatch.setattr(blobs, "S3Store", _boom)


@pytest.fixture
async def tool_client(
    settings: Settings,
    store: Store,
    tool_harness: FakeHarness,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=tool_harness, token=worker_secret
    ) as (_app, client, _worker):
        _block_storage(monkeypatch)
        yield client


async def _agent_with_echo(client: AsyncClient, token: str) -> str:
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "echo",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def test_function_tool_requires_action(
    tool_client: AsyncClient, tool_harness: FakeHarness
) -> None:
    token = "tools"
    agent_id = await _agent_with_echo(tool_client, token)
    created = await tool_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "use echo",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "requires_action"
    actions = body["required_actions"]
    assert actions == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "echo",
            "arguments": {"text": "hi"},
        }
    ]
    assert tool_harness.function_tools is not None
    assert tool_harness.function_tools[0]["name"] == "echo"
    session_id = body["id"]
    events = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.requires_action" in types
    assert types[-1] == "agent.session.requires_action"
    require = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    ]
    turn_id = require[0]["data"]["turn_id"]
    items = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth(token)
    )
    kinds = [item["type"] for item in items.json()["data"]]
    assert kinds == ["message", "function_call"]

    resumed = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={
            "type": "agent.session.input.tool_result",
            "turn_id": turn_id,
            "call_id": "call_1",
            "success": True,
            "output": "pong",
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "idle"
    assert resumed.json()["required_actions"] == []
    events = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["pong"]
    types = [event["type"] for event in events.json()["data"]]
    assert types[-1] == "agent.session.idle"


async def test_completed_turn_log_counts_function_tools(
    tool_client: AsyncClient, store: Store
) -> None:
    token = "tools"
    agent_id = await _agent_with_echo(tool_client, token)
    created = await tool_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "use echo",
        },
    )
    session_id = created.json()["id"]
    events = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    require = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    ]
    turn_id = require[0]["data"]["turn_id"]
    resumed = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={
            "type": "agent.session.input.tool_result",
            "turn_id": turn_id,
            "call_id": "call_1",
            "success": True,
            "output": "pong",
        },
    )
    assert resumed.status_code == 200
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        row = await get_turn_log(db, tenant_id, uuid.UUID(turn_id))
    assert row is not None
    assert row.status == "completed"
    assert row.tool_names == ["echo"]
    assert row.tool_counts == {"echo": 1}
    blob = str(row.tool_names) + str(row.tool_counts) + str(row.mcp_names)
    assert "use echo" not in blob
    assert "pong" not in blob


async def test_tool_result_wrong_tenant_is_404(tool_client: AsyncClient) -> None:
    token_a = "a"
    token_b = "b"
    agent_id = await _agent_with_echo(tool_client, token_a)
    created = await tool_client.post(
        "/v1/agents/sessions",
        headers=_auth(token_a),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "use echo",
        },
    )
    session_id = created.json()["id"]
    events = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token_a)
    )
    require = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    ]
    turn_id = require[0]["data"]["turn_id"]
    other = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token_b),
        json={
            "type": "agent.session.input.tool_result",
            "turn_id": turn_id,
            "call_id": "call_1",
            "success": True,
            "output": "nope",
        },
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"
