import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.fake_runner import AsgiWebsocket, connect_runner

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert response.status_code == 200
    return str(response.json()["id"])


async def _app_client(
    settings: Settings, store: Store, harness: FakeHarness
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield app, client


async def test_self_hosted_create_returns_id_and_key(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for app, client in _app_client(settings, store, harness):
        del app
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["status"] == "idle"
        assert body["environment"]["type"] == "self_hosted"
        env_id = body["environment_id"]
        assert env_id == body["environment"]["id"]
        uuid.UUID(env_id)
        assert isinstance(body["key"], str) and body["key"]
        assert body["required_actions"] == [
            {"type": "environment_connection", "environment_id": env_id}
        ]
        events = await client.get(
            f"/v1/agents/sessions/{body['id']}/events", headers=_auth(token)
        )
        types = [event["type"] for event in events.json()["data"]]
        assert types == [
            "agent.session.created",
            "agent.session.environment.pending",
            "agent.session.idle",
        ]
        got = await client.get(
            f"/v1/agents/sessions/{body['id']}", headers=_auth(token)
        )
        assert "key" not in got.json()
        assert got.json()["environment"]["id"] == env_id
        assert got.json()["required_actions"] == body["required_actions"]


async def test_self_hosted_runs_without_runner(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for _app, client in _app_client(settings, store, harness):
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "environment": {"type": "self_hosted"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["status"] == "idle"
        assert body["required_actions"] == [
            {
                "type": "environment_connection",
                "environment_id": body["environment_id"],
            }
        ]
        assert harness.tools is False
        events = await client.get(
            f"/v1/agents/sessions/{body['id']}/events", headers=_auth(token)
        )
        types = [event["type"] for event in events.json()["data"]]
        assert types[0] == "agent.session.created"
        assert types[1] == "agent.session.environment.pending"
        assert types[-1] == "agent.session.idle"
        assert "agent.session.environment.connected" not in types
        texts = [
            event["data"]["text"]
            for event in events.json()["data"]
            if event["type"] == "agent.session.turn.output_text.done"
        ]
        assert texts == ["hello"]


async def test_fake_runner_connects_and_speaks_protocol(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for app, client in _app_client(settings, store, harness):
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        body = created.json()
        env_id = body["environment_id"]
        key = body["key"]
        session_id = body["id"]
        async with connect_runner(app, env_id, key) as runner:
            events = await client.get(
                f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
            )
            types = [event["type"] for event in events.json()["data"]]
            assert "agent.session.environment.pending" in types
            assert types[-1] == "agent.session.environment.connected"
            got = await client.get(
                f"/v1/agents/sessions/{session_id}", headers=_auth(token)
            )
            assert got.json()["required_actions"] == []
            hub = app.state.env_hub
            ping = await hub.call(uuid.UUID(env_id), "ping")
            assert ping["ok"] is True
            executed = await hub.call(uuid.UUID(env_id), "exec", command="echo hi")
            assert executed["ok"] is True
            assert executed["stdout"] == "echo hi"
            written = await hub.call(
                uuid.UUID(env_id), "write", path="note.txt", content="hello"
            )
            assert written["ok"] is True
            read = await hub.call(uuid.UUID(env_id), "read", path="note.txt")
            assert read["content"] == "hello"
            edited = await hub.call(
                uuid.UUID(env_id),
                "edit",
                path="note.txt",
                old_text="hello",
                new_text="hi",
            )
            assert edited["ok"] is True
            listed = await hub.call(uuid.UUID(env_id), "list", path="")
            assert listed["names"] == ["note.txt"]
            published = await hub.call(uuid.UUID(env_id), "artifact", path="note.txt")
            assert published["ok"] is True
            assert runner.files["note.txt"] == "hi"
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        types = [event["type"] for event in events.json()["data"]]
        assert types[-1] == "agent.session.environment.disconnected"
        got = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth(token)
        )
        assert got.json()["required_actions"] == [
            {"type": "environment_connection", "environment_id": env_id}
        ]


async def test_self_hosted_connected_runs_computer_tools(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    harness.computer_calls = [
        {
            "name": "write",
            "call_id": "w1",
            "arguments": {"path": "note.txt", "content": "hello"},
        },
        {
            "name": "edit",
            "call_id": "e1",
            "arguments": {"path": "note.txt", "old_text": "hello", "new_text": "hi"},
        },
        {
            "name": "bash",
            "call_id": "b1",
            "arguments": {"command": "echo hi"},
        },
        {
            "name": "read",
            "call_id": "r1",
            "arguments": {"path": "note.txt"},
        },
    ]
    async for app, client in _app_client(settings, store, harness):
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        body = created.json()
        session_id = body["id"]
        async with connect_runner(app, body["environment_id"], body["key"]) as runner:
            turned = await client.post(
                f"/v1/agents/sessions/{session_id}/events",
                headers=_auth(token),
                json={"type": "agent.session.input.message", "content": "use files"},
            )
            assert turned.status_code == 200
            assert turned.json()["status"] == "idle"
            assert harness.tools is True
            assert runner.files["note.txt"] == "hi"
            events = await client.get(
                f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
            )
            added = [
                event
                for event in events.json()["data"]
                if event["type"] == "agent.session.turn.item.added"
                and event["data"].get("item_type") == "command_execution"
            ]
            names = [event["data"]["name"] for event in added]
            assert names == ["write", "edit", "bash", "read"]
            assert "agent.session.requires_action" not in [
                event["type"] for event in events.json()["data"]
            ]


async def test_self_hosted_disconnect_turns_tools_off(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for app, client in _app_client(settings, store, harness):
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        body = created.json()
        session_id = body["id"]
        env_id = body["environment_id"]
        async with connect_runner(app, env_id, body["key"]):
            pass
        harness.computer_calls = [
            {
                "name": "write",
                "call_id": "w1",
                "arguments": {"path": "gone.txt", "content": "nope"},
            }
        ]
        turned = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "after drop"},
        )
        assert turned.status_code == 200
        assert harness.tools is False
        assert turned.json()["required_actions"] == [
            {"type": "environment_connection", "environment_id": env_id}
        ]
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        assert [
            event
            for event in events.json()["data"]
            if event["data"].get("item_type") == "command_execution"
        ] == []


async def test_self_hosted_tool_use_then_harvest(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    harness.computer_calls = [
        {
            "name": "write",
            "call_id": "w1",
            "arguments": {
                "path": "outputs/note.txt",
                "content": "hello",
            },
        }
    ]
    async for app, client in _app_client(settings, store, harness):
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        body = created.json()
        session_id = body["id"]
        async with connect_runner(app, body["environment_id"], body["key"]) as runner:
            turned = await client.post(
                f"/v1/agents/sessions/{session_id}/events",
                headers=_auth(token),
                json={"type": "agent.session.input.message", "content": "write out"},
            )
            assert turned.status_code == 200
            assert runner.files["outputs/note.txt"] == "hello"
            from apipi.pi.artifacts import harvest_session

            async with store.session() as db:
                await harvest_session(
                    db,
                    settings,
                    uuid.UUID(session_id),
                    None,
                    app.state.env_hub,
                )
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


async def test_self_hosted_artifact_content_via_runner(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for app, client in _app_client(settings, store, harness):
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        body = created.json()
        session_id = body["id"]
        env_id = body["environment_id"]
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
        )
        assert listed.json() == {"data": []}
        async with connect_runner(app, env_id, body["key"]) as runner:
            runner.files["artifacts/note.txt"] = "skip"
            runner.files["outputs/note.txt"] = "hello"
            from apipi.pi.artifacts import harvest_session

            async with store.session() as db:
                await harvest_session(
                    db,
                    settings,
                    uuid.UUID(session_id),
                    None,
                    app.state.env_hub,
                )
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
        )
        data = listed.json()["data"]
        assert [item["path"] for item in data] == ["outputs/note.txt"]
        artifact_id = data[0]["id"]
        content = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
            headers=_auth(token),
        )
        assert content.status_code == 200
        assert content.content == b"hello"


async def test_self_hosted_wrong_key_is_not_found(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for app, client in _app_client(settings, store, harness):
        token = "t"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        env_id = created.json()["environment_id"]
        ws = AsgiWebsocket(app, f"/v1/environments/{env_id}")
        accepted = await ws.connect()
        assert accepted["type"] == "websocket.accept"
        await ws.send_json({"type": "hello", "key": "nope"})
        reply = await ws.receive_json()
        assert reply["ok"] is False
        assert reply["error"] == "not_found"
        await ws.close()


async def test_self_hosted_wrong_tenant_is_404(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for _app, client in _app_client(settings, store, harness):
        agent_id = await _agent(client, "a")
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("a"),
            json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
        )
        session_id = created.json()["id"]
        env_id = created.json()["environment_id"]
        listed = await client.get("/v1/agents/sessions", headers=_auth("b"))
        assert listed.json() == {"data": []}
        got = await client.get(f"/v1/agents/sessions/{session_id}", headers=_auth("b"))
        assert got.status_code == 404
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth("b")
        )
        assert events.status_code == 404
        content = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts/{env_id}/content",
            headers=_auth("b"),
        )
        assert content.status_code == 404


async def test_no_runners_resource(settings: Settings, store: Store) -> None:
    harness = FakeHarness()
    async for _app, client in _app_client(settings, store, harness):
        token = "t"
        listed = await client.get("/v1/runners", headers=_auth(token))
        assert listed.status_code == 404
        created = await client.post("/v1/runners", headers=_auth(token), json={})
        assert created.status_code == 404
