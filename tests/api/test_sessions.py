import asyncio
import json
import logging
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from apipi.api.sessions import _event_stream
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.errors import ApiError
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import (
    PUBLIC_EVENT_TYPES,
    EventHub,
    FakeHarness,
    persist_event,
)
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import SessionRow
from apipi.store.repo import (
    create_session,
    create_tenant,
    create_turn,
    get_session_by_id,
    update_session,
)


def _token(name: str = "t") -> str:
    return name


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


async def test_health_is_not_request_logged(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.http")
    response = await client.get("/health")
    assert response.status_code == 200
    assert not any(record.name == "apipi.http" for record in caplog.records)


async def test_request_and_turn_are_logged(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    token = _token()
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
    http = [record for record in caplog.records if record.name == "apipi.http"]
    assert http
    starts = [record for record in http if record.getMessage() == "request start"]
    assert starts
    assert starts[0].__dict__["method"] == "POST"
    assert any(
        record.__dict__.get("route") == "/v1/agents/sessions" for record in starts
    )
    last = http[-1]
    assert last.getMessage() == "request"
    assert last.__dict__["method"] == "POST"
    assert last.__dict__["status"] == 200
    assert last.__dict__.get("request_id")
    assert any(
        record.name == "apipi" and record.getMessage() == "turn start"
        for record in caplog.records
    )
    turns = [
        record
        for record in caplog.records
        if record.name == "apipi" and record.getMessage() == "turn"
    ]
    assert turns
    assert turns[-1].__dict__["status"] == "completed"
    assert int(turns[-1].__dict__["latency_ms"]) >= 0


async def test_session_crud_environment_none(client: AsyncClient) -> None:
    token = _token()
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
    assert body["environment"] == {"type": "none", "sandbox_size": "S"}
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


async def test_inline_agent_is_not_saved(client: AsyncClient) -> None:
    token = _token()
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


async def test_unknown_environment_type(client: AsyncClient) -> None:
    token = _token()
    agent_id = await _create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "foo"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "not_implemented"
    assert response.json()["error"]["code"] == "foo"


async def test_hosted_alias_is_openai_hosted(client: AsyncClient) -> None:
    token = _token()
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "hosted"}},
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["type"] == "openai_hosted"
    assert Path(env["directory"]).is_dir()
    got = await client.get(
        f"/v1/agents/sessions/{created.json()['id']}", headers=_auth(token)
    )
    assert got.json()["environment"]["type"] == "openai_hosted"


async def test_default_environment_is_openai_hosted(client: AsyncClient) -> None:
    token = _token()
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


async def test_fake_harness_determined_events(client: AsyncClient) -> None:
    token = _token()
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
    assert "agent.session.turn.output_text.delta" not in types
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
    token = _token()
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


async def test_output_text_delta_is_live_only(store: Store) -> None:
    hub = EventHub()
    async with store.session() as db:
        tenant = await create_tenant(db, name="a")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id

    parsed: list[dict[str, object]] = []
    finished = asyncio.Event()

    async def consume() -> None:
        agen = _event_stream(store, hub, tenant_id, session_id, None)
        try:
            async for chunk in agen:
                if chunk.startswith(":"):
                    continue
                parsed.extend(_parse_sse(chunk))
                types = [event["type"] for event in parsed]
                if "agent.session.turn.output_text.done" in types:
                    finished.set()
                    return
        finally:
            await agen.aclose()

    task = asyncio.create_task(consume())
    for _ in range(100):
        if session_id in hub._subs:
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        raise AssertionError("stream did not subscribe")
    async with store.session() as db:
        live = await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.output_text.delta",
            data={"delta": "hi"},
        )
        done = await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.output_text.done",
            data={"text": "hi"},
        )
    assert live is None
    assert done is not None
    assert done.seq == 1
    await asyncio.wait_for(finished.wait(), timeout=2)
    await task
    types = [event["type"] for event in parsed]
    assert types == [
        "agent.session.turn.output_text.delta",
        "agent.session.turn.output_text.done",
    ]
    assert "seq" not in parsed[0]
    assert parsed[1]["seq"] == 1
    async with store.session() as db:
        stored = await list_events(db, tenant_id, session_id)
    assert [event.type for event in stored] == ["agent.session.turn.output_text.done"]
    assert stored[0].seq == 1


async def test_thinking_events_are_stored_and_replayed(store: Store) -> None:
    hub = EventHub()
    async with store.session() as db:
        tenant = await create_tenant(db, name="a")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
        started = await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.thinking.started",
            data={"item_id": "t1", "content_index": 0},
        )
        completed = await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.thinking.completed",
            data={
                "item_id": "t1",
                "content_index": 0,
                "duration_ms": 40,
                "reasoning_tokens": 3,
                "preview": "plan",
                "preview_truncated": False,
            },
        )
        dropped = await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.thinking.delta",
            data={"delta": "full secret thinking"},
        )
        await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.idle",
            data={},
        )
    assert started is not None
    assert completed is not None
    assert dropped is None
    streamed = _parse_sse(
        await _read_stream_until_idle(store, EventHub(), tenant_id, session_id)
    )
    assert [event["type"] for event in streamed] == [
        "agent.session.turn.thinking.started",
        "agent.session.turn.thinking.completed",
        "agent.session.idle",
    ]
    data = streamed[1]["data"]
    assert isinstance(data, dict)
    assert data.get("preview") == "plan"
    assert "full secret thinking" not in json.dumps(streamed)


async def test_turn_publishes_live_delta(settings: Settings, store: Store) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    hub: EventHub = app.state.event_hub
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = _token()
        agent_id = await _create_agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        session_id = uuid.UUID(created.json()["id"])
        queue = hub.subscribe(session_id)
        posted = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "hello"},
        )
        assert posted.status_code == 200
    live_types = []
    while not queue.empty():
        live_types.append(queue.get_nowait()["type"])
    assert "agent.session.turn.output_text.delta" in live_types
    async with store.session() as db:
        row = await db.scalar(select(SessionRow).where(SessionRow.id == session_id))
        assert row is not None
        stored = await list_events(db, row.tenant_id, session_id)
    assert "agent.session.turn.output_text.delta" not in [
        event.type for event in stored
    ]
    assert "agent.session.turn.output_text.done" in [event.type for event in stored]


async def test_cross_tenant_session_is_404(client: AsyncClient) -> None:
    token_a = _token("a")
    token_b = _token("b")
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


async def test_unknown_session_field(client: AsyncClient) -> None:
    token = _token()
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


async def test_failed_turn_logs_event_and_code(
    client: AsyncClient, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="apipi")
    token = _token()
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    sid = uuid.UUID(created.json()["id"])
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        await update_session(db, tenant_id, sid, changes={"status": "in_progress"})
        turn = await create_turn(db, tenant_id, sid, status="in_progress")
        turn_id = turn.id
    got = await client.get(f"/v1/agents/sessions/{sid}", headers=_auth(token))
    assert got.status_code == 200
    failed = [
        record
        for record in caplog.records
        if record.name == "apipi" and record.__dict__.get("event") == "turn.failed"
    ]
    assert failed
    last = failed[-1]
    assert last.levelno == logging.ERROR
    assert last.__dict__["error_code"] == "turn_interrupted"
    assert last.__dict__["tenant_id"] == str(tenant_id)
    assert last.__dict__["session_id"] == str(sid)
    assert last.__dict__["turn_id"] == str(turn_id)


async def test_get_session_recovers_stale_in_progress(
    client: AsyncClient, store: Store
) -> None:
    token = _token()
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    sid = uuid.UUID(created.json()["id"])
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        await update_session(db, tenant_id, sid, changes={"status": "in_progress"})
        await create_turn(db, tenant_id, sid, status="in_progress")
    got = await client.get(f"/v1/agents/sessions/{sid}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["status"] == "idle"
    events = await client.get(f"/v1/agents/sessions/{sid}/events", headers=_auth(token))
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.turn.failed" in types
    assert types[-1] == "agent.session.idle"


async def test_follow_up_on_stale_in_progress_starts_turn(
    client: AsyncClient, store: Store
) -> None:
    token = _token()
    agent_id = await _create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    sid = uuid.UUID(created.json()["id"])
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        await update_session(db, tenant_id, sid, changes={"status": "in_progress"})
        await create_turn(db, tenant_id, sid, status="in_progress")
    posted = await client.post(
        f"/v1/agents/sessions/{sid}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "hello"},
    )
    assert posted.status_code == 200
    assert posted.json()["status"] == "idle"
    turns = await client.get(f"/v1/agents/sessions/{sid}/turns", headers=_auth(token))
    statuses = [row["status"] for row in turns.json()["data"]]
    assert "failed" in statuses


async def test_stream_create_ends_when_first_turn_fails(
    settings: Settings, store: Store
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())

    async def fail_turn(*_args: object, **_kwargs: object) -> None:
        raise ApiError(
            "invalid_request",
            "Too many live sessions",
            code="capacity",
            status_code=429,
        )

    app.state.execution.run_turn = fail_turn
    token = _token("stream-fail")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _create_agent(client, token)
        async with client.stream(
            "POST",
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "environment": {"type": "none"},
                "input": "hello",
                "stream": True,
            },
            timeout=5,
        ) as response:
            assert response.status_code == 200
            body = await response.aread()
    events = _parse_sse(body.decode())
    types = [event["type"] for event in events]
    assert types[0] == "agent.session.created"
    assert "agent.session.error" in types
    assert types[-1] == "agent.session.failed"
    error = next(event for event in events if event["type"] == "agent.session.error")
    data = error["data"]
    assert isinstance(data, dict)
    assert data["code"] == "capacity"
    assert data["message"] == "Too many live sessions"


async def test_delete_stops_guest_before_dropping_the_row(
    settings: Settings, store: Store
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    seen: dict[str, bool] = {}

    async def teardown(session_id: uuid.UUID) -> None:
        async with store.session() as db:
            row = await get_session_by_id(db, session_id)
        seen["row"] = row is not None

    app.state.execution.teardown = teardown
    token = _token("delete-order")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _create_agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        assert created.status_code == 200
        session_id = created.json()["id"]
        deleted = await client.delete(
            f"/v1/agents/sessions/{session_id}", headers=_auth(token)
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        missing = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth(token)
        )
        assert missing.status_code == 404
    assert seen["row"] is True
