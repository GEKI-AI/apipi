import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def _worker_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        worker_token="worker-secret",
    )


async def _agent(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    return str(agent.json()["id"])


@pytest.mark.parametrize(
    ("kind", "pools", "want"),
    [
        ("chat", "chat", "chat"),
        ("chat", "microvm", None),
        ("chat", "both", "chat"),
        ("computer", "chat", None),
        ("computer", "microvm", "microvm"),
        ("computer", "both", "microvm"),
        ("agents_none", "chat", "chat"),
        ("agents_none", "microvm", None),
        ("agents_none", "both", "chat"),
    ],
)
async def test_placement_matrix(
    settings: Settings,
    store: Store,
    kind: str,
    pools: str,
    want: str | None,
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
    token = f"matrix-{kind}-{pools}"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _agent(client, token)
        if kind == "chat":
            created = await client.post(
                "/v1/chat/sessions",
                headers=_auth(token),
                json={"agent_id": agent_id},
            )
        elif kind == "computer":
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth(token),
                json={
                    "agent_id": agent_id,
                    "environment": {"type": "openai_hosted"},
                },
            )
        else:
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth(token),
                json={"agent_id": agent_id, "environment": {"type": "none"}},
            )
        assert created.status_code == 200
        session_id = uuid.UUID(created.json()["id"])
        workers: list[FakeWorker] = []
        if pools in {"chat", "both"}:
            chat = FakeWorker(app, "worker-secret")
            await chat.connect(capacity=4, run_mode="chat")
            workers.append(chat)
        if pools in {"microvm", "both"}:
            microvm = FakeWorker(app, "worker-secret")
            await microvm.connect(capacity=4, run_mode="microvm")
            workers.append(microvm)
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        if want is None:
            assert command is None
        else:
            assert command is not None
            assert command["payload"]["run_mode"] == want
            holder = None
            for worker in workers:
                live = app.state.workers.get(uuid.UUID(str(worker.worker_id)))
                if live is not None and uuid.UUID(command["lease_id"]) in live.leases:
                    holder = live
            assert holder is not None
            assert holder.run_mode == want
        for worker in workers:
            await worker.close()


async def test_chat_facade_does_not_leak_environment(client: AsyncClient) -> None:
    token = "facade"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "input": "hello"},
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    listed = await client.get("/v1/chat/sessions", headers=_auth(token))
    got = await client.get(f"/v1/chat/sessions/{session_id}", headers=_auth(token))
    events = await client.get(
        f"/v1/chat/sessions/{session_id}/events", headers=_auth(token)
    )
    turns = await client.get(
        f"/v1/chat/sessions/{session_id}/turns", headers=_auth(token)
    )
    items = await client.get(
        f"/v1/chat/sessions/{session_id}/items", headers=_auth(token)
    )
    exported = await client.get(
        f"/v1/chat/sessions/{session_id}/export", headers=_auth(token)
    )
    for body in (
        created.json(),
        got.json(),
        *listed.json()["data"],
    ):
        assert "environment" not in body
    dumped = json.dumps(
        {
            "created": created.json(),
            "got": got.json(),
            "list": listed.json(),
            "events": events.json(),
            "turns": turns.json(),
            "items": items.json(),
            "export": exported.json(),
        }
    )
    assert '"environment"' not in dumped
    assert "openai_hosted" not in dumped


async def test_chat_rejects_computer_and_bash_tools(client: AsyncClient) -> None:
    token = "deny"
    agent_id = await _agent(client, token)
    hosted = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "openai_hosted"},
        },
    )
    assert hosted.status_code == 400
    assert hosted.json()["error"]["code"] == "unknown_field"
    bash = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={
            "agent": {
                "name": "bot",
                "model": "test",
                "tools": [{"type": "bash", "name": "bash"}],
            }
        },
    )
    assert bash.status_code == 400
    stdio = await client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={
            "agent": {
                "name": "bot",
                "model": "test",
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "shell",
                        "transport": {"type": "stdio", "command": "bash"},
                    }
                ],
            }
        },
    )
    assert stdio.status_code == 400
    assert stdio.json()["error"]["code"] == "chat_tool"
