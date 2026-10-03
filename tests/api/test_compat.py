import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import select
from tests.support.split_worker import split_client_for
from tests.support.workspace import hosted_dir

from apipi.api.sessions import _event_stream
from apipi.common.event_bus import EventHub
from apipi.config import Settings
from apipi.protocol import PUBLIC_EVENT_TYPES
from apipi.store.engine import Store
from apipi.store.models import SessionRow
from apipi.worker.fake_harness import FAKE_USAGE, FakeHarness

_TOOLS = [
    {
        "type": "function",
        "name": "echo",
        "description": "echo",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "mcp",
        "server_label": "tavily",
        "server_url": "https://mcp.tavily.com/mcp",
        "headers": {"Authorization": "Bearer x"},
    },
    {
        "type": "mcp",
        "server_label": "docs",
        "server_url": "https://mcp.example.com/mcp",
        "allowed_tools": ["search"],
    },
]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error(response: Response) -> dict[str, object]:
    payload = response.json()
    assert set(payload) == {"error"}
    error = payload["error"]
    assert set(error) == {"type", "code", "message"}
    return error


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
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


async def _read_stream_until_seq(
    store: Store,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    last_seq: int,
) -> str:
    agen = _event_stream(store, hub, tenant_id, session_id, None)
    chunks: list[str] = []
    try:
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            chunks.append(chunk)
            parsed = _parse_sse("".join(chunks))
            if parsed and int(parsed[-1]["seq"]) >= last_seq:
                return "".join(chunks)
    finally:
        await agen.aclose()
    return "".join(chunks)


async def _agent(client: AsyncClient, token: str, **fields: object) -> str:
    body: dict[str, object] = {"name": "bot", "model": "test", **fields}
    created = await client.post("/v1/agents", headers=_auth(token), json=body)
    assert created.status_code == 200
    return str(created.json()["id"])


async def _session(
    client: AsyncClient,
    token: str,
    *,
    agent_id: str,
    environment: dict[str, Any] | None = None,
    input: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"agent_id": agent_id}
    if environment is not None:
        payload["environment"] = environment
    if input is not None:
        payload["input"] = input
    created = await client.post(
        "/v1/agents/sessions", headers=_auth(token), json=payload
    )
    assert created.status_code == 200
    return created.json()


async def test_compat_agents_crud(client: AsyncClient) -> None:
    token = "compat-agents"
    created = await client.post(
        "/v1/agents",
        headers={**_auth(token), "OpenAI-Beta": "agents=v1"},
        json={
            "name": "one",
            "model": "test-model",
            "instructions": "be brief",
            "metadata": {"k": "v"},
            "tools": _TOOLS,
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "one"
    assert body["model"] == "test-model"
    assert body["instructions"] == "be brief"
    assert body["metadata"] == {"k": "v"}
    assert [tool["type"] for tool in body["tools"]] == ["function", "mcp", "mcp"]
    agent_id = body["id"]
    listed = await client.get("/v1/agents", headers=_auth(token))
    assert [row["id"] for row in listed.json()["data"]] == [agent_id]
    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert got.json()["id"] == agent_id
    updated = await client.post(
        f"/v1/agents/{agent_id}", headers=_auth(token), json={"name": "two"}
    )
    assert updated.json()["name"] == "two"
    deleted = await client.delete(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert deleted.json() == {"id": agent_id, "deleted": True}
    gone = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert gone.status_code == 404


async def test_compat_sessions_stream_follow_up(
    store: Store, client: AsyncClient
) -> None:
    token = "compat-sessions"
    agent_id = await _agent(client, token)
    created = await _session(
        client,
        token,
        agent_id=agent_id,
        environment={"type": "none"},
        input="hello",
    )
    session_id = created["id"]
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert events.status_code == 200
    types = [event["type"] for event in events.json()["data"]]
    assert types[0] == "agent.session.created"
    assert types[-1] == "agent.session.idle"
    follow = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert follow.status_code == 200
    assert follow.json()["status"] == "idle"
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    stored = events.json()["data"]
    texts = [
        event["data"]["text"]
        for event in stored
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["hello", "again"]
    last_seq = int(stored[-1]["seq"])
    async with store.session() as db:
        row = await db.scalar(
            select(SessionRow).where(SessionRow.id == uuid.UUID(session_id))
        )
        assert row is not None
        tenant_id = row.tenant_id
        sid = row.id
    streamed = _parse_sse(
        await _read_stream_until_seq(store, EventHub(), tenant_id, sid, last_seq)
    )
    stream_types = [event["type"] for event in streamed]
    assert stream_types[0] == "agent.session.created"
    assert stream_types[-1] == "agent.session.idle"
    stream_texts = [
        event["data"]["text"]
        for event in streamed
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert stream_texts == ["hello", "again"]


async def test_compat_environment_openai_hosted(
    client: AsyncClient, settings: Settings
) -> None:
    token = "compat-hosted"
    agent_id = await _agent(client, token)
    created = await _session(client, token, agent_id=agent_id)
    env = created["environment"]
    assert env["type"] == "openai_hosted"
    assert hosted_dir(settings, token, created["id"]).is_dir()


async def test_compat_environment_hosted_alias(
    client: AsyncClient, settings: Settings
) -> None:
    token = "compat-hosted-alias"
    agent_id = await _agent(client, token)
    created = await _session(
        client, token, agent_id=agent_id, environment={"type": "hosted"}
    )
    assert created["environment"]["type"] == "openai_hosted"
    assert hosted_dir(settings, token, created["id"]).is_dir()


async def test_compat_environment_none(client: AsyncClient) -> None:
    token = "compat-none"
    agent_id = await _agent(client, token)
    created = await _session(
        client, token, agent_id=agent_id, environment={"type": "none"}
    )
    assert created["environment"] == {
        "type": "none",
        "sandbox_size": "S",
        "sandbox_image": "default",
        "container_size": "small",
        "sandbox": None,
    }
    assert created["status"] == "idle"


async def test_compat_environment_self_hosted_not_supported(
    client: AsyncClient,
) -> None:
    token = "compat-self"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
    )
    assert response.status_code == 400
    error = _error(response)
    assert error["type"] == "not_implemented"
    assert "self_hosted" in str(error["message"])


async def test_compat_function_tools(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "compat-tools"
        agent_id = await _agent(client, token, tools=[_TOOLS[0]])
        created = await _session(
            client,
            token,
            agent_id=agent_id,
            environment={"type": "none"},
            input="use echo",
        )
        assert created["status"] == "requires_action"
        assert created["required_actions"] == [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "echo",
                "arguments": {"text": "hi"},
            }
        ]
        session_id = created["id"]
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        require = [
            event
            for event in events.json()["data"]
            if event["type"] == "agent.session.requires_action"
        ]
        resumed = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={
                "type": "agent.session.input.tool_result",
                "turn_id": require[0]["data"]["turn_id"],
                "call_id": "call_1",
                "success": True,
                "output": "pong",
            },
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "idle"


async def test_compat_mcp(client: AsyncClient) -> None:
    token = "compat-mcp"
    agent_id = await _agent(client, token, tools=_TOOLS[1:])
    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    tools = got.json()["tools"]
    assert tools[0]["server_url"] == "https://mcp.tavily.com/mcp"
    assert tools[1]["allowed_tools"] == ["search"]


async def test_compat_skills(
    settings: Settings, store: Store, tmp_path: Path, worker_secret: str
) -> None:
    caps = tmp_path / "pack"
    tree = caps / "cap-skill"
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text("---\nname: cap-skill\n---\n")
    async with split_client_for(settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "compat-skills"
        agent_id = await _agent(client, token)
        created = await _session(
            client,
            token,
            agent_id=agent_id,
            environment={
                "type": "openai_hosted",
                "capability_directories": [str(caps)],
            },
            input="hello",
        )
        env = created["environment"]
        assert env["capability_directories"] == [str(caps)]
        copied = (
            hosted_dir(settings, token, created["id"])
            / "pack"
            / "cap-skill"
            / "SKILL.md"
        )
        assert copied.is_file()


async def test_compat_artifacts(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = "compat-artifacts"
    agent_id = await _agent(client, token)
    created = await _session(client, token, agent_id=agent_id)
    session_id = created["id"]
    directory = hosted_dir(settings, token, session_id)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    posted = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "text": "hello"},
    )
    assert posted.status_code == 200
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    artifact_id = listed.json()["data"][0]["id"]
    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 200
    assert content.content == b"hello"


async def test_compat_usage_on_turns(client: AsyncClient) -> None:
    token = "compat-usage"
    agent_id = await _agent(client, token)
    created = await _session(
        client,
        token,
        agent_id=agent_id,
        environment={"type": "none"},
        input="hello",
    )
    session_id = created["id"]
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    completed = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.completed"
    ]
    assert completed[0]["data"]["usage"] == FAKE_USAGE
    assert "cost" not in completed[0]["data"]["usage"]
    turn_id = completed[0]["data"]["turn_id"]
    one = await client.get(
        f"/v1/agents/sessions/{session_id}/turns/{turn_id}",
        headers=_auth(token),
    )
    assert one.json()["usage"] == FAKE_USAGE


async def test_compat_session_export(client: AsyncClient) -> None:
    token = "compat-export"
    agent_id = await _agent(client, token)
    created = await _session(
        client,
        token,
        agent_id=agent_id,
        environment={"type": "none"},
        input="hello",
    )
    session_id = created["id"]
    exported = await client.get(
        f"/v1/apipi/sessions/{session_id}/export", headers=_auth(token)
    )
    assert exported.status_code == 200
    body = exported.json()
    types = [event["type"] for event in body["events"]]
    assert types[0] == "agent.session.created"
    assert types[-1] == "agent.session.idle"
    assert set(types) <= PUBLIC_EVENT_TYPES
    assert len(body["turns"]) == 1
    assert [item["data"]["role"] for item in body["items"]] == ["user", "assistant"]


async def test_compat_event_types(client: AsyncClient) -> None:
    token = "compat-events"
    agent_id = await _agent(client, token)
    created = await _session(
        client,
        token,
        agent_id=agent_id,
        environment={"type": "none"},
        input="hello",
    )
    events = await client.get(
        f"/v1/agents/sessions/{created['id']}/events", headers=_auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    assert set(types) <= PUBLIC_EVENT_TYPES
    assert "agent.session.created" in types
    assert "agent.session.turn.created" in types
    assert "agent.session.turn.completed" in types
    assert "agent.session.turn.output_text.delta" not in types
    assert "agent.session.turn.output_text.done" in types
    assert types[-1] == "agent.session.idle"


async def test_compat_error_envelope(client: AsyncClient) -> None:
    token = "compat-errors"
    unknown = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "one", "vaults": []}
    )
    assert unknown.status_code == 400
    assert _error(unknown)["type"] == "invalid_request"
    assert _error(unknown)["code"] == "unknown_field"
    blocked = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "one", "multi_agent": True}
    )
    assert blocked.status_code == 400
    assert _error(blocked)["type"] == "not_implemented"
    assert _error(blocked)["code"] == "multi_agent"
    missing = await client.get(f"/v1/agents/{uuid.uuid4()}", headers=_auth(token))
    assert missing.status_code == 404
    assert _error(missing)["type"] == "invalid_request"
    assert _error(missing)["code"] == "not_found"


async def test_compat_tenant_404(client: AsyncClient) -> None:
    token_a = "compat-a"
    token_b = "compat-b"
    agent_id = await _agent(client, token_a)
    created = await _session(
        client, token_a, agent_id=agent_id, environment={"type": "none"}
    )
    session_id = created["id"]
    agent = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token_b))
    session = await client.get(
        f"/v1/agents/sessions/{session_id}", headers=_auth(token_b)
    )
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token_b)
    )
    assert agent.status_code == 404
    assert session.status_code == 404
    assert events.status_code == 404
    assert _error(agent)["code"] == "not_found"
    assert _error(session)["code"] == "not_found"
    assert _error(events)["code"] == "not_found"


def _nested_message(text: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                ],
            }
        ]
    }


async def test_compat_nested_events_message(client: AsyncClient) -> None:
    token = "compat-nested-message"
    agent_id = await _agent(client, token)
    created = await _session(
        client,
        token,
        agent_id=agent_id,
        environment={"type": "none"},
        input="hello",
    )
    session_id = created["id"]
    follow = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json=_nested_message("again"),
    )
    assert follow.status_code == 200
    assert follow.json()["status"] == "idle"
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["hello", "again"]


async def test_compat_nested_events_tool_result(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "compat-nested-tool"
        agent_id = await _agent(client, token, tools=[_TOOLS[0]])
        created = await _session(
            client,
            token,
            agent_id=agent_id,
            environment={"type": "none"},
            input="use echo",
        )
        session_id = created["id"]
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        require = [
            event
            for event in events.json()["data"]
            if event["type"] == "agent.session.requires_action"
        ]
        resumed = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={
                "events": [
                    {
                        "type": "agent.session.input.tool_result",
                        "turn_id": require[0]["data"]["turn_id"],
                        "call_id": "call_1",
                        "success": True,
                        "output": "pong",
                    }
                ]
            },
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "idle"


async def test_compat_nested_events_cancel(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    harness.hold = True
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        token = "compat-nested-cancel"
        agent_id = await _agent(client, token)
        created = await _session(
            client, token, agent_id=agent_id, environment={"type": "none"}
        )
        session_id = created["id"]
        sid = uuid.UUID(session_id)
        hub = app.state.event_hub
        queue = hub.subscribe(sid)
        try:
            task = asyncio.create_task(
                client.post(
                    f"/v1/agents/sessions/{session_id}/events",
                    headers=_auth(token),
                    json=_nested_message("go"),
                )
            )
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=2)
                if event["type"] == "agent.session.in_progress":
                    break
            cancelled = await client.post(
                f"/v1/agents/sessions/{session_id}/events",
                headers=_auth(token),
                json={"events": [{"type": "agent.session.input.cancel"}]},
            )
            assert cancelled.status_code == 200
            posted = await task
            assert posted.status_code == 200
            assert posted.json()["status"] == "idle"
        finally:
            hub.unsubscribe(sid, queue)


async def test_compat_nested_events_rejects(client: AsyncClient) -> None:
    token = "compat-nested-reject"
    agent_id = await _agent(client, token)
    created = await _session(
        client, token, agent_id=agent_id, environment={"type": "none"}, input="hello"
    )
    session_id = created["id"]
    path = f"/v1/agents/sessions/{session_id}/events"
    multi = await client.post(
        path,
        headers=_auth(token),
        json={
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "a"}],
                        }
                    ],
                },
                {
                    "type": "agent.session.input.message",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "b"}],
                        }
                    ],
                },
            ]
        },
    )
    assert multi.status_code == 400
    assert _error(multi)["type"] == "invalid_request"
    mixed = await client.post(
        path,
        headers=_auth(token),
        json={
            "type": "agent.session.input.message",
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "x"}],
                        }
                    ],
                }
            ],
        },
    )
    assert mixed.status_code == 400
    assert _error(mixed)["type"] == "invalid_request"
    extra = await client.post(
        path,
        headers=_auth(token),
        json={
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "x"}],
                        }
                    ],
                    "foo": 1,
                }
            ]
        },
    )
    assert extra.status_code == 400
    assert _error(extra)["code"] == "unknown_field"
    image = await client.post(
        path,
        headers=_auth(token),
        json={
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_image"}],
                        }
                    ],
                }
            ]
        },
    )
    assert image.status_code == 400
    assert _error(image)["type"] == "invalid_request"


_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # noqa: E501


async def test_image_requires_registry_capability(client: AsyncClient) -> None:
    token = "compat-image-off"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "input": {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG}",
                    }
                ],
            },
        },
    )
    assert created.status_code == 400
    assert _error(created)["code"] == "unsupported_input"


async def test_image_is_stored_by_file_id(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    vision = settings.model_copy(
        update={
            "model_registry": {"test": {"input": ["text", "image"], "reasoning": True}}
        }
    )
    async with split_client_for(vision, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "compat-image-on"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "see"},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/png;base64,{_PNG}",
                            },
                            {"type": "input_text", "text": "this"},
                        ],
                    }
                ],
            },
        )
        assert created.status_code == 200
        session_id = created.json()["id"]
        items = await client.get(
            f"/v1/agents/sessions/{session_id}/items", headers=_auth(token)
        )
        listed = await client.get("/v1/apipi/models", headers=_auth(token))
    assert items.status_code == 200
    content = items.json()["data"][0]["data"]["content"]
    assert content[0] == {"type": "input_text", "text": "see"}
    assert content[1]["type"] == "input_image"
    assert content[1]["file_id"].startswith("file-")
    assert "base64" not in json.dumps(content)
    assert content[2]["text"] == "this"
    assert listed.json()["data"][0]["id"] == "test"
    assert "image" in listed.json()["data"][0]["input"]
