import asyncio
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.support.http import auth, create_agent, parse_sse, tenant_of

from apipi.api.sessions import _event_stream
from apipi.common.errors import ApiError
from apipi.common.event_bus import EventHub
from apipi.config import Settings
from apipi.services.session_events import persist_event
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    create_turn,
    get_session_by_id,
    set_session_lease,
    update_session,
)


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
            types = [event["type"] for event in parse_sse("".join(chunks))]
            if types and types[-1] == "agent.session.idle":
                return "".join(chunks)
    finally:
        await agen.aclose()
    return "".join(chunks)


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
    token = "t"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
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
    token = "t"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "metadata": {"k": "v"},
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "idle"
    assert body["environment"] == {
        "type": "none",
        "sandbox_size": "S",
        "sandbox_image": "default",
        "container_size": "small",
        "sandbox": None,
    }
    assert body["agent_id"] == agent_id
    assert body["metadata"] == {"k": "v"}
    assert body["required_actions"] == []
    session_id = body["id"]

    listed = await client.get("/v1/agents/sessions", headers=auth(token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [session_id]

    got = await client.get(f"/v1/agents/sessions/{session_id}", headers=auth(token))
    assert got.status_code == 200
    assert got.json()["id"] == session_id

    updated = await client.post(
        f"/v1/agents/sessions/{session_id}",
        headers=auth(token),
        json={"metadata": {"k": "2"}},
    )
    assert updated.status_code == 200
    assert updated.json()["metadata"] == {"k": "2"}

    deleted = await client.delete(
        f"/v1/agents/sessions/{session_id}", headers=auth(token)
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"id": session_id, "deleted": True}
    gone = await client.get(f"/v1/agents/sessions/{session_id}", headers=auth(token))
    assert gone.status_code == 404


async def test_inline_agent_is_not_saved(client: AsyncClient) -> None:
    token = "t"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {"name": "inline", "model": "test"},
            "environment": {"type": "none"},
        },
    )
    assert created.status_code == 200
    assert created.json()["agent_id"] is None
    agents = await client.get("/v1/agents", headers=auth(token))
    assert agents.json() == {"data": []}


async def test_unknown_environment_type(client: AsyncClient) -> None:
    token = "t"
    agent_id = await create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "foo"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "not_implemented"
    assert response.json()["error"]["code"] == "foo"


async def test_sse_replays_persisted_events(store: Store, client: AsyncClient) -> None:
    token = "t"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "hi",
        },
    )
    session_id = created.json()["id"]
    stored = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    stored_types = [event["type"] for event in stored.json()["data"]]
    assert stored_types[0] == "agent.session.created"

    async with store.session() as db:
        row = await db.scalar(select(SessionRow))
        assert row is not None
        tenant_id = row.tenant_id
        sid = row.id
    streamed = parse_sse(
        await _read_stream_until_idle(store, EventHub(), tenant_id, sid)
    )
    assert [event["type"] for event in streamed] == stored_types

    last_seq = stored.json()["data"][-1]["seq"]
    replay = parse_sse(
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
                parsed.extend(parse_sse(chunk))
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
    streamed = parse_sse(
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


async def test_failed_turn_logs_event_and_code(
    client: AsyncClient, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="apipi")
    token = "t"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    sid = uuid.UUID(created.json()["id"])
    tenant_id = tenant_of(token)
    async with store.session() as db:
        await update_session(db, tenant_id, sid, changes={"status": "in_progress"})
        turn = await create_turn(db, tenant_id, sid, status="in_progress")
        turn_id = turn.id
    got = await client.get(f"/v1/agents/sessions/{sid}", headers=auth(token))
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
    token = "t"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    sid = uuid.UUID(created.json()["id"])
    tenant_id = tenant_of(token)
    async with store.session() as db:
        await update_session(db, tenant_id, sid, changes={"status": "in_progress"})
        await create_turn(db, tenant_id, sid, status="in_progress")
    got = await client.get(f"/v1/agents/sessions/{sid}", headers=auth(token))
    assert got.status_code == 200
    assert got.json()["status"] == "idle"
    events = await client.get(f"/v1/agents/sessions/{sid}/events", headers=auth(token))
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.turn.failed" in types
    assert types[-1] == "agent.session.idle"


async def test_follow_up_on_stale_in_progress_starts_turn(
    client: AsyncClient, store: Store
) -> None:
    token = "t"
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    sid = uuid.UUID(created.json()["id"])
    tenant_id = tenant_of(token)
    async with store.session() as db:
        await update_session(db, tenant_id, sid, changes={"status": "in_progress"})
        await create_turn(db, tenant_id, sid, status="in_progress")
    posted = await client.post(
        f"/v1/agents/sessions/{sid}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "hello"},
    )
    assert posted.status_code == 200
    assert posted.json()["status"] == "idle"
    turns = await client.get(f"/v1/agents/sessions/{sid}/turns", headers=auth(token))
    statuses = [row["status"] for row in turns.json()["data"]]
    assert "failed" in statuses


async def _stale_leased_session(
    client: AsyncClient,
    store: Store,
    token: str,
    *,
    worker_id: uuid.UUID,
    lease_id: uuid.UUID,
    lease_in: timedelta,
) -> tuple[str, uuid.UUID, uuid.UUID]:
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    sid = uuid.UUID(created.json()["id"])
    tenant_id = tenant_of(token)
    async with store.session() as db:
        await update_session(db, tenant_id, sid, changes={"status": "in_progress"})
        stale = await create_turn(db, tenant_id, sid, status="in_progress")
        await set_session_lease(
            db,
            tenant_id,
            sid,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + lease_in,
        )
    return token, sid, stale.id


async def _follow_up(client: AsyncClient, token: str, sid: uuid.UUID) -> Any:
    return await client.post(
        f"/v1/agents/sessions/{sid}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "hello"},
    )


async def _turn_statuses(
    client: AsyncClient, token: str, sid: uuid.UUID
) -> dict[str, str]:
    turns = await client.get(f"/v1/agents/sessions/{sid}/turns", headers=auth(token))
    return {row["id"]: row["status"] for row in turns.json()["data"]}


async def test_follow_up_on_expired_orphaned_lease_fails_fast(
    client: AsyncClient, store: Store
) -> None:
    """An expired lease no worker holds must not block a follow-up turn.

    The lease holder is unknown to the hub and the lease has expired, so
    the `turn.cancel` command is undelivered. The API must release the
    lease and fail the stale turn immediately instead of blocking until
    `turn_timeout` for worker events that will never arrive.
    """
    token, sid, stale = await _stale_leased_session(
        client,
        store,
        "orphaned-lease",
        worker_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_in=timedelta(minutes=-5),
    )
    start = time.monotonic()
    posted = await _follow_up(client, token, sid)
    assert posted.status_code == 200
    assert posted.json()["status"] == "idle"
    assert time.monotonic() - start < 60
    by_id = await _turn_statuses(client, token, sid)
    assert by_id[str(stale)] == "failed"
    assert "completed" in set(by_id.values())


async def test_follow_up_keeps_live_lease_on_other_replica(
    client: AsyncClient, store: Store
) -> None:
    """A live lease whose worker socket is not on this replica is kept.

    The worker may still be running the turn, so the follow-up gets the
    usual 429 and neither the lease nor the turn is touched.
    """
    lease_id = uuid.uuid4()
    token, sid, stale = await _stale_leased_session(
        client,
        store,
        "live-lease-elsewhere",
        worker_id=uuid.uuid4(),
        lease_id=lease_id,
        lease_in=timedelta(minutes=5),
    )
    posted = await _follow_up(client, token, sid)
    assert posted.status_code == 429
    assert "Worker socket is on" in posted.text
    async with store.session() as db:
        row = await get_session_by_id(db, sid)
    assert row is not None
    assert row.lease_id == lease_id
    assert (await _turn_statuses(client, token, sid))[str(stale)] == "in_progress"


async def test_follow_up_on_live_lease_without_running_turn_is_bounded(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live worker that holds the lease but runs no turn must not hang.

    The worker accepts `turn.cancel` yet has nothing to cancel, so it
    emits no events. The follow-up waits a bounded grace, then releases
    the lease (API bookkeeping included), fails the stale turn and runs
    the new turn.
    """
    from tests.support.split_worker import split_client_for

    from apipi.workerhub import execution as execution_module

    monkeypatch.setattr(execution_module, "CANCEL_GRACE", timedelta(seconds=0.3))
    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        _worker,
    ):
        (conn,) = app.state.workers._conns.values()
        lease_id = uuid.uuid4()
        token, sid, stale = await _stale_leased_session(
            client,
            store,
            "live-lease-no-turn",
            worker_id=conn.worker_id,
            lease_id=lease_id,
            lease_in=timedelta(minutes=5),
        )
        conn.leases.add(lease_id)
        start = time.monotonic()
        posted = await _follow_up(client, token, sid)
        assert posted.status_code == 200
        assert posted.json()["status"] == "idle"
        assert time.monotonic() - start < 4
        assert lease_id not in conn.leases
        by_id = await _turn_statuses(client, token, sid)
        assert by_id[str(stale)] == "failed"
        assert "completed" in set(by_id.values())


async def test_follow_up_after_worker_restart_releases_lease(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    """A reconnected worker that no longer holds the lease fails fast.

    The worker socket is on this replica but `conn.leases` lacks the
    lease, so the cancel is undelivered: the lease is orphaned and is
    released without waiting for a grace period.
    """
    from tests.support.split_worker import split_client_for

    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        _worker,
    ):
        (conn,) = app.state.workers._conns.values()
        token, sid, stale = await _stale_leased_session(
            client,
            store,
            "restarted-worker",
            worker_id=conn.worker_id,
            lease_id=uuid.uuid4(),
            lease_in=timedelta(minutes=5),
        )
        posted = await _follow_up(client, token, sid)
        assert posted.status_code == 200
        assert posted.json()["status"] == "idle"
        by_id = await _turn_statuses(client, token, sid)
        assert by_id[str(stale)] == "failed"


async def test_stream_create_ends_when_first_turn_fails(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    from tests.support.split_worker import split_client_for

    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        _worker,
    ):

        async def fail_turn(*_args: object, **_kwargs: object) -> None:
            raise ApiError(
                "invalid_request",
                "Too many live sessions",
                code="capacity",
                status_code=429,
            )

        app.state.execution.run_turn = fail_turn
        token = "stream-fail"
        agent_id = await create_agent(client, token)
        async with client.stream(
            "POST",
            "/v1/agents/sessions",
            headers=auth(token),
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
    events = parse_sse(body.decode())
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
    settings: Settings, store: Store, worker_secret: str
) -> None:
    from tests.support.split_worker import split_client_for

    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        _worker,
    ):
        seen: dict[str, bool] = {}

        async def teardown(session_id: uuid.UUID) -> None:
            async with store.session() as db:
                row = await get_session_by_id(db, session_id)
            seen["row"] = row is not None

        app.state.execution.teardown = teardown
        token = "delete-order"
        agent_id = await create_agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={"agent_id": agent_id, "environment": {"type": "none"}},
        )
        assert created.status_code == 200
        session_id = created.json()["id"]
        deleted = await client.delete(
            f"/v1/agents/sessions/{session_id}", headers=auth(token)
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        missing = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=auth(token)
        )
        assert missing.status_code == 404
    assert seen["row"] is True
