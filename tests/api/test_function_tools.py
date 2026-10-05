import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update
from tests.support.http import auth, tenant_of
from tests.support.postgres import needs_postgres
from tests.support.split_worker import (
    SplitWorker,
    block_storage,
    wait_for_event_types,
)

from apipi.config import Settings
from apipi.gateway.tokens import hash_token
from apipi.services.turn_state import fail_stale_in_progress
from apipi.store.engine import Store
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import clear_session_lease, get_session
from apipi.store.turn_logs import get_turn_log
from apipi.worker.fake_harness import FakeHarness


class _OneTurnCalls(FakeHarness):
    async def generate(
        self, text: str, **kwargs: Any
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        async for event in super().generate(text, **kwargs):
            yield event
            if event[0] == "function_call":
                calls, self.function_calls = self.function_calls, []
                for call in calls:
                    yield ("function_call", dict(call))


@pytest.fixture
def tool_harness() -> FakeHarness:
    harness = _OneTurnCalls()
    harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    return harness


def _record_placements(
    app: FastAPI, placements: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = app.state.workers
    for name in ("acquire", "command"):
        real = getattr(hub, name)

        async def record(
            *args: Any, _name: str = name, _real: Any = real, **kwargs: Any
        ) -> Any:
            placements.append((_name, kwargs["op"]))
            return await _real(*args, **kwargs)

        monkeypatch.setattr(hub, name, record)


@pytest.fixture
def placements() -> list[tuple[str, str]]:
    return []


@pytest.fixture
async def tool_split(
    settings: Settings,
    store: Store,
    tool_harness: FakeHarness,
    worker_secret: str,
    placements: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[FastAPI, AsyncClient, SplitWorker]]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=tool_harness, token=worker_secret
    ) as (app, client, worker):
        block_storage(monkeypatch)
        _record_placements(app, placements, monkeypatch)
        yield app, client, worker


@pytest.fixture
def tool_client(tool_split: tuple[FastAPI, AsyncClient, SplitWorker]) -> AsyncClient:
    return tool_split[1]


async def _agent_with_echo(client: AsyncClient, token: str) -> str:
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
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
        headers=auth(token),
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
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
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
        f"/v1/agents/sessions/{session_id}/items", headers=auth(token)
    )
    kinds = [item["type"] for item in items.json()["data"]]
    assert kinds == ["message", "function_call"]

    resumed = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
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
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
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
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "use echo",
        },
    )
    session_id = created.json()["id"]
    events = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    require = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    ]
    turn_id = require[0]["data"]["turn_id"]
    resumed = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
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
        headers=auth(token_a),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "use echo",
        },
    )
    session_id = created.json()["id"]
    events = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token_a)
    )
    require = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    ]
    turn_id = require[0]["data"]["turn_id"]
    other = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token_b),
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


async def _waiting_session(client: AsyncClient, token: str) -> tuple[str, str]:
    agent_id = await _agent_with_echo(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "use echo",
        },
    )
    assert created.json()["status"] == "requires_action"
    session_id = created.json()["id"]
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    turn_id = next(
        event["data"]["turn_id"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    )
    return session_id, turn_id


def _tool_result(turn_id: str, call_id: str = "call_1") -> dict[str, Any]:
    return {
        "type": "agent.session.input.tool_result",
        "turn_id": turn_id,
        "call_id": call_id,
        "success": True,
        "output": "pong",
    }


_NOT_REQUIRES_ACTION = {
    "type": "invalid_request",
    "code": "invalid_request",
    "message": "Session is not requires_action",
}


async def test_tool_result_on_new_idle_session_is_400(
    tool_client: AsyncClient, placements: list[tuple[str, str]]
) -> None:
    token = "tools"
    agent_id = await _agent_with_echo(tool_client, token)
    created = await tool_client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.json()["status"] == "idle"
    response = await tool_client.post(
        f"/v1/agents/sessions/{created.json()['id']}/events",
        headers=auth(token),
        json=_tool_result(str(uuid.uuid4())),
    )
    assert response.status_code == 400
    assert response.json()["error"] == _NOT_REQUIRES_ACTION
    assert placements == []


async def test_tool_result_after_the_turn_finished_is_400(
    tool_client: AsyncClient, placements: list[tuple[str, str]]
) -> None:
    token = "tools"
    session_id, turn_id = await _waiting_session(tool_client, token)
    resumed = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json=_tool_result(turn_id),
    )
    assert resumed.json()["status"] == "idle"
    before = list(placements)
    again = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json=_tool_result(turn_id),
    )
    assert again.status_code == 400
    assert again.json()["error"] == _NOT_REQUIRES_ACTION
    assert placements == before


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "turn_id",
            "00000000-0000-0000-0000-000000000002",
            "turn_id is not the turn waiting for a tool result",
        ),
        ("call_id", "call_9", "call_id is not waiting for a tool result"),
    ],
    ids=["turn_id", "call_id"],
)
async def test_tool_result_for_a_call_that_is_not_waiting_is_400(
    tool_client: AsyncClient,
    placements: list[tuple[str, str]],
    field: str,
    value: str,
    message: str,
) -> None:
    token = "tools"
    session_id, turn_id = await _waiting_session(tool_client, token)
    before = list(placements)
    rejected = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={**_tool_result(turn_id), field: value},
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"] == {
        "type": "invalid_request",
        "code": "invalid_request",
        "message": message,
    }
    assert placements == before
    session = await tool_client.get(
        f"/v1/agents/sessions/{session_id}", headers=auth(token)
    )
    assert session.json()["status"] == "requires_action"
    assert [action["call_id"] for action in session.json()["required_actions"]] == [
        "call_1"
    ]
    resumed = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json=_tool_result(turn_id),
    )
    assert resumed.json()["status"] == "idle"


async def test_tool_result_with_other_calls_open_answers_at_once(
    tool_client: AsyncClient, tool_harness: FakeHarness
) -> None:
    tool_harness.function_calls.append(
        {"name": "echo", "arguments": {"text": "ho"}, "call_id": "call_2"}
    )
    token = "tools"
    session_id, turn_id = await _waiting_session(tool_client, token)
    first = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json=_tool_result(turn_id),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "requires_action"
    assert [action["call_id"] for action in first.json()["required_actions"]] == [
        "call_2"
    ]
    events = await tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    waits = [
        event["data"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    ]
    assert [data["turn_id"] for data in waits] == [turn_id, turn_id]
    assert [action["call_id"] for action in waits[-1]["required_actions"]] == ["call_2"]
    second = await tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json=_tool_result(turn_id, "call_2"),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "idle"
    assert second.json()["required_actions"] == []


async def _lose_lease(
    app: FastAPI, store: Store, worker: SplitWorker, session_id: str, how: str
) -> None:
    sid = uuid.UUID(session_id)
    if how == "released":
        await worker.execution.note_stopped(sid)
        return
    async with store.session() as db:
        if how == "cleared":
            await clear_session_lease(db, tenant_of("tools"), sid)
            return
        await db.execute(
            update(SessionRow)
            .where(SessionRow.id == sid)
            .values(lease_until=utc_now() - timedelta(seconds=1))
        )
    assert await app.state.workers.expire(store, app.state.event_hub) == [sid]


async def _ended(
    client: AsyncClient, token: str, session_id: str
) -> tuple[list[tuple[str, str]], int]:
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    data = events.json()["data"]
    failed = [
        (event["data"]["turn_id"], event["data"]["code"])
        for event in data
        if event["type"] == "agent.session.turn.failed"
    ]
    idle = [event for event in data if event["type"] == "agent.session.idle"]
    return failed, len(idle)


@pytest.mark.parametrize("how", ["released", "expired", "cleared"])
async def test_waiting_turn_without_a_lease_is_interrupted(
    tool_split: tuple[FastAPI, AsyncClient, SplitWorker],
    store: Store,
    placements: list[tuple[str, str]],
    how: str,
) -> None:
    app, client, worker = tool_split
    token = "tools"
    session_id, turn_id = await _waiting_session(client, token)
    await _lose_lease(app, store, worker, session_id, how)
    if how == "released":
        await wait_for_event_types(client, token, session_id, "agent.session.idle")
    if how != "cleared":
        assert await _ended(client, token, session_id) == (
            [(turn_id, "turn_interrupted")],
            1,
        )
    before = list(placements)
    rejected = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json=_tool_result(turn_id),
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"] == _NOT_REQUIRES_ACTION
    assert placements == before
    assert await _ended(client, token, session_id) == (
        [(turn_id, "turn_interrupted")],
        1,
    )
    follow = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert follow.status_code == 200
    assert follow.json()["status"] == "idle"
    assert placements[len(before) :] == [
        ("command", "turn.start"),
        ("acquire", "turn.start"),
    ]


@pytest.mark.parametrize("read", ["get", "list"])
async def test_session_read_ends_a_waiting_turn_without_a_lease(
    tool_split: tuple[FastAPI, AsyncClient, SplitWorker], store: Store, read: str
) -> None:
    app, client, worker = tool_split
    token = "tools"
    session_id, turn_id = await _waiting_session(client, token)
    await _lose_lease(app, store, worker, session_id, "cleared")
    if read == "get":
        response = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=auth(token)
        )
        session = response.json()
    else:
        response = await client.get("/v1/agents/sessions", headers=auth(token))
        session = next(
            row for row in response.json()["data"] if row["id"] == session_id
        )
    assert response.status_code == 200
    assert session["status"] == "idle"
    assert session["required_actions"] == []
    assert await _ended(client, token, session_id) == (
        [(turn_id, "turn_interrupted")],
        1,
    )


@pytest.mark.parametrize(
    "calls", ["one_after_another", pytest.param("at_once", marks=needs_postgres)]
)
async def test_waiting_turn_without_a_lease_is_interrupted_once(
    tool_split: tuple[FastAPI, AsyncClient, SplitWorker], store: Store, calls: str
) -> None:
    app, client, worker = tool_split
    token = "tools"
    session_id, turn_id = await _waiting_session(client, token)
    await _lose_lease(app, store, worker, session_id, "cleared")
    path = f"/v1/agents/sessions/{session_id}"
    if calls == "at_once":
        reads = await asyncio.gather(
            client.get(path, headers=auth(token)),
            client.get(path, headers=auth(token)),
        )
        assert [read.status_code for read in reads] == [200, 200]
        assert [read.json()["status"] for read in reads] == ["idle", "idle"]
    else:
        sid = uuid.UUID(session_id)
        async with store.session() as db:
            row = await get_session(db, tenant_of(token), sid)
            assert row is not None
            assert row.status == "requires_action"
            read = await client.get(path, headers=auth(token))
            assert read.json()["status"] == "idle"
            await fail_stale_in_progress(db, app.state.event_hub, tenant_of(token), sid)
    assert await _ended(client, token, session_id) == (
        [(turn_id, "turn_interrupted")],
        1,
    )


async def test_expiry_ends_the_other_turns_when_one_cannot_be_ended(
    tool_split: tuple[FastAPI, AsyncClient, SplitWorker],
    store: Store,
    tool_harness: FakeHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apipi.workerhub import hub as hub_module

    app, client, _worker = tool_split
    token = "tools"
    broken_id, broken_turn = await _waiting_session(client, token)
    tool_harness.function_calls.append(
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    )
    other_id, other_turn = await _waiting_session(client, token)
    sessions = [uuid.UUID(broken_id), uuid.UUID(other_id)]
    real = hub_module.fail_stale_in_progress

    async def fail_one(
        db: Any, bus: Any, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> Any:
        if session_id == sessions[0]:
            raise RuntimeError("turn log write failed")
        return await real(db, bus, tenant_id, session_id)

    monkeypatch.setattr(hub_module, "fail_stale_in_progress", fail_one)
    async with store.session() as db:
        await db.execute(
            update(SessionRow)
            .where(SessionRow.id.in_(sessions))
            .values(lease_until=utc_now() - timedelta(seconds=1))
        )
    expired = await app.state.workers.expire(store, app.state.event_hub)
    assert sorted(expired) == sorted(sessions)
    async with store.session() as db:
        for session_id in sessions:
            row = await get_session(db, tenant_of(token), session_id)
            assert row is not None
            assert row.lease_id is None
    assert await _ended(client, token, other_id) == (
        [(other_turn, "turn_interrupted")],
        1,
    )
    assert await _ended(client, token, broken_id) == ([], 0)
    assert [
        getattr(record, "session_id", None)
        for record in caplog.records
        if getattr(record, "event", None) == "worker.lease.turn_end_failed"
    ] == [str(sessions[0])]
    read = await client.get(f"/v1/agents/sessions/{broken_id}", headers=auth(token))
    assert read.json()["status"] == "idle"
    assert await _ended(client, token, broken_id) == (
        [(broken_turn, "turn_interrupted")],
        1,
    )
