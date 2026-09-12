from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tool_harness() -> FakeHarness:
    harness = FakeHarness()
    harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    return harness


@pytest.fixture
async def tool_client(
    settings: Settings, store: Store, tool_harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=tool_harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
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


async def test_tool_result_unknown_field(tool_client: AsyncClient) -> None:
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
    response = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={
            "type": "agent.session.input.tool_result",
            "turn_id": turn_id,
            "call_id": "call_1",
            "success": True,
            "output": "pong",
            "foo": 1,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_field"


async def test_tool_result_missing_session_is_404(tool_client: AsyncClient) -> None:
    response = await tool_client.post(
        "/v1/agents/sessions/00000000-0000-0000-0000-000000000001/events",
        headers=_auth("tools"),
        json={
            "type": "agent.session.input.tool_result",
            "turn_id": "00000000-0000-0000-0000-000000000002",
            "call_id": "call_1",
            "success": True,
            "output": "pong",
        },
    )
    assert response.status_code == 404
