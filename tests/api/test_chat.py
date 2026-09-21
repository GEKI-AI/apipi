from httpx import AsyncClient


def _token(name: str = "t") -> str:
    return name


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent_id(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    return str(agent.json()["id"])


async def test_chat_create_hides_environment(client: AsyncClient) -> None:
    token = _token()
    agent_id = await _agent_id(client, token)
    created = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "input": "hello"},
    )
    assert created.status_code == 200
    body = created.json()
    assert "environment" not in body
    assert body["metadata"]["apipi.session_kind"] == "chat"
    assert body["status"] == "idle"
    session_id = body["id"]
    got = await client.get(f"/v1/chat/sessions/{session_id}", headers=_auth(token))
    assert got.status_code == 200
    assert "environment" not in got.json()
    events = await client.get(
        f"/v1/chat/sessions/{session_id}/events", headers=_auth(token)
    )
    assert events.status_code == 200
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.created" in types
    turns = await client.get(
        f"/v1/chat/sessions/{session_id}/turns", headers=_auth(token)
    )
    assert turns.status_code == 200
    assert turns.json()["data"]


async def test_chat_rejects_environment_field(client: AsyncClient) -> None:
    token = _token()
    agent_id = await _agent_id(client, token)
    created = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "openai_hosted"},
        },
    )
    assert created.status_code == 400
    assert created.json()["error"]["code"] == "unknown_field"


async def test_chat_list_skips_agent_sessions(client: AsyncClient) -> None:
    token = _token()
    agent_id = await _agent_id(client, token)
    hosted = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "openai_hosted"}},
    )
    assert hosted.status_code == 200
    chat = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id},
    )
    assert chat.status_code == 200
    listed = await client.get("/v1/chat/sessions", headers=_auth(token))
    assert listed.status_code == 200
    ids = [row["id"] for row in listed.json()["data"]]
    assert chat.json()["id"] in ids
    assert hosted.json()["id"] not in ids
    assert all("environment" not in row for row in listed.json()["data"])
    missing = await client.get(
        f"/v1/chat/sessions/{hosted.json()['id']}", headers=_auth(token)
    )
    assert missing.status_code == 404


async def test_chat_events_and_export(client: AsyncClient) -> None:
    token = _token()
    agent_id = await _agent_id(client, token)
    created = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id},
    )
    session_id = created.json()["id"]
    posted = await client.post(
        f"/v1/chat/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "hi"},
    )
    assert posted.status_code == 200
    exported = await client.get(
        f"/v1/chat/sessions/{session_id}/export", headers=_auth(token)
    )
    assert exported.status_code == 200
    assert "events" in exported.json()
    assert "environment" not in exported.json()


_STDIO = {
    "type": "mcp",
    "server_label": "local",
    "transport": {"type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp"]},
}
_HTTP = {
    "type": "mcp",
    "server_label": "search",
    "transport": {"type": "http", "server_url": "https://mcp.example/mcp"},
}
_FN = {"type": "function", "name": "echo", "parameters": {"type": "object"}}


async def test_chat_allows_function_tools(client: AsyncClient) -> None:
    token = _token()
    created = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent": {"name": "bot", "model": "test", "tools": [_FN]}},
    )
    assert created.status_code == 200
    assert "environment" not in created.json()


async def test_chat_rejects_stdio_mcp(client: AsyncClient) -> None:
    token = _token()
    created = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent": {"name": "bot", "model": "test", "tools": [_STDIO]}},
    )
    assert created.status_code == 400
    assert created.json()["error"]["code"] == "chat_tool"


async def test_chat_rejects_stdio_on_saved_agent(client: AsyncClient) -> None:
    token = _token()
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "tools": [_STDIO]},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent_id": agent.json()["id"]},
    )
    assert created.status_code == 400
    assert created.json()["error"]["code"] == "chat_tool"


async def test_chat_profile_agent_rejects_stdio(client: AsyncClient) -> None:
    token = _token()
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.session_kind": "chat"},
            "tools": [_STDIO],
        },
    )
    assert agent.status_code == 400
    assert agent.json()["error"]["code"] == "chat_tool"
    ok = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.session_kind": "chat"},
            "tools": [_FN, _HTTP],
        },
    )
    assert ok.status_code == 200
    patched = await client.post(
        f"/v1/agents/{ok.json()['id']}",
        headers=_auth(token),
        json={"tools": [_STDIO]},
    )
    assert patched.status_code == 400
    assert patched.json()["error"]["code"] == "chat_tool"
