import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.prom import metric_line
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import list_turns, set_session_lease


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def _worker_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
    )


async def _session(
    app: FastAPI, token: str, store: Store
) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        assert agent.status_code == 200
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
            },
        )
        assert created.status_code == 200
        body = created.json()
    return _tenant(token), uuid.UUID(body["id"])


async def _lease(
    store: Store,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> uuid.UUID:
    async with store.session() as db:
        lease_id = uuid.uuid4()
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + timedelta(seconds=30),
        )
        return lease_id


def _envelope(
    session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    turn_id = payload.get("turn_id")
    if turn_id is None and isinstance(payload.get("data"), dict):
        maybe = payload["data"].get("turn_id")
        turn_id = maybe if isinstance(maybe, str) else None
    return {
        "v": 2,
        "session_id": str(session_id),
        "turn_id": turn_id,
        "seq": seq,
        "type": type,
        "payload": payload,
    }


async def _turn_envelopes(
    session_id: uuid.UUID, turn_id: uuid.UUID, start: int = 1
) -> list[dict[str, Any]]:
    item_id = uuid.uuid4()
    flow = [
        ("session.status", {"status": "in_progress"}),
        ("event", {"type": "agent.session.in_progress", "data": {}}),
        ("turn.status", {"turn_id": str(turn_id), "status": "started"}),
        (
            "event",
            {
                "type": "agent.session.turn.created",
                "data": {"turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        (
            "item.added",
            {
                "item_id": str(item_id),
                "item_type": "message",
                "turn_id": str(turn_id),
                "data": {"role": "user", "content": "hi"},
            },
        ),
        (
            "event",
            {
                "type": "agent.session.turn.item.added",
                "data": {
                    "item_id": str(item_id),
                    "item_type": "message",
                    "turn_id": str(turn_id),
                },
                "turn_id": str(turn_id),
            },
        ),
        (
            "event",
            {
                "type": "agent.session.turn.item.done",
                "data": {"item_id": str(item_id), "turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        ("turn.status", {"turn_id": str(turn_id), "status": "completed"}),
        (
            "usage",
            {
                "turn_id": str(turn_id),
                "status": "completed",
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        ),
        (
            "event",
            {
                "type": "agent.session.turn.completed",
                "data": {"turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        ("session.status", {"status": "idle"}),
        ("event", {"type": "agent.session.idle", "data": {}}),
    ]
    return [
        _envelope(session_id, start + index, type, payload)
        for index, (type, payload) in enumerate(flow)
    ]


async def _drain_acks(worker: FakeWorker, count: int) -> list[dict[str, Any]]:
    acks = []
    while len(acks) < count:
        message = await worker.receive_json(timeout=10)
        if message.get("type") == "ack":
            acks.append(message)
    return acks


async def test_socket_ingest_acks_and_persists(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(
        api_settings_for(_worker_settings(settings), batch_window_zero=False),
        store=store,
    )
    token = "t"
    tenant_id, session_id = await _session(app, token, store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    worker_id = uuid.UUID(str(hello["worker_id"]))
    await _lease(store, tenant_id, session_id, worker_id)
    turn_id = uuid.uuid4()
    for envelope in await _turn_envelopes(session_id, turn_id):
        await worker.send_json(envelope)
    acks = await _drain_acks(worker, 1)
    assert acks[-1]["session_id"] == str(session_id)
    assert acks[-1]["last_seq"] == 12
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    assert [event.type for event in events][-6:] == [
        "agent.session.in_progress",
        "agent.session.turn.created",
        "agent.session.turn.item.added",
        "agent.session.turn.item.done",
        "agent.session.turn.completed",
        "agent.session.idle",
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        assert listed.status_code == 200
        assert len(listed.json()["data"]) >= 6
    await worker.close()


async def test_socket_reject_counts_and_acks_past(
    settings: Settings,
    store: Store,
    worker_secret: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        api_settings_for(
            Settings(
                database_url=settings.database_url,
                run_mode="none",
                sessions_dir=settings.sessions_dir,
                metrics=True,
            ),
            batch_window_zero=False,
        ),
        store=store,
    )
    token = "t"
    tenant_id, session_id = await _session(app, token, store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    worker_id = uuid.UUID(str(hello["worker_id"]))
    await _lease(store, tenant_id, session_id, worker_id)
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    await worker.send_json(
        _envelope(session_id, 1, "event", {"type": "agent.session.nope", "data": {}})
    )
    await worker.send_json(
        _envelope(session_id, 2, "session.status", {"status": "idle"})
    )
    acks = await _drain_acks(worker, 1)
    assert acks[-1]["last_seq"] == 2
    assert "worker envelope rejected" in caplog.text
    assert any(
        getattr(record, "error_code", None) == "unknown_event"
        for record in caplog.records
    )
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
        assert "agent.session.nope" not in [event.type for event in events]
        assert not [
            event for event in events if event.type.startswith("agent.session.turn.")
        ]
        from apipi.store.repo import get_session

        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
        assert row.worker_seq == 2
    assert app.state.metrics is not None
    body = app.state.metrics.scrape().decode()
    assert metric_line(body, "apipi_worker_protocol_total", event="envelope_rejected")
    await worker.close()


@pytest.mark.parametrize("another_replica", [False, True])
async def test_reconnect_replays_exactly_once(
    settings: Settings, store: Store, worker_secret: str, another_replica: bool
) -> None:
    app = create_app(
        api_settings_for(_worker_settings(settings), batch_window_zero=False),
        store=store,
    )
    token = "t"
    tenant_id, session_id = await _session(app, token, store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    worker_id = uuid.UUID(str(hello["worker_id"]))
    lease_id = await _lease(store, tenant_id, session_id, worker_id)
    turn_id = uuid.uuid4()
    flow = await _turn_envelopes(session_id, turn_id)
    for envelope in flow[:6]:
        await worker.send_json(envelope)
    acks = await _drain_acks(worker, 1)
    assert acks[-1]["last_seq"] == 6
    await worker.close()
    if another_replica:
        app = create_app(
            api_settings_for(_worker_settings(settings), batch_window_zero=False),
            store=store,
        )
    second = FakeWorker(app, worker_secret, worker_id=str(worker_id))
    hello = await second.connect(
        running=[
            {
                "session_id": str(session_id),
                "lease_id": str(lease_id),
                "last_seq": 6,
            }
        ]
    )
    assert hello.get("ok") is True
    assert hello["sessions"][str(session_id)] == 6
    for envelope in flow:
        await second.send_json(envelope)
    acks = await _drain_acks(second, 1)
    assert acks[-1]["last_seq"] == 12
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
        turns = await list_turns(db, tenant_id, session_id)
    types = [event.type for event in events]
    assert types[types.index("agent.session.in_progress") :] == [
        "agent.session.in_progress",
        "agent.session.turn.created",
        "agent.session.turn.item.added",
        "agent.session.turn.item.done",
        "agent.session.turn.completed",
        "agent.session.idle",
    ]
    assert turns is not None and len(turns) == 1
    await second.close()


async def test_ingest_latency_within_batch_window(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    assert settings.worker_ingest_batch_window.total_seconds() <= 0.05
    app = create_app(
        api_settings_for(_worker_settings(settings), batch_window_zero=False),
        store=store,
    )
    token = "t"
    tenant_id, session_id = await _session(app, token, store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    await _lease(store, tenant_id, session_id, uuid.UUID(str(hello["worker_id"])))
    turn_id = uuid.uuid4()
    flow = await _turn_envelopes(session_id, turn_id)
    latencies = []
    for envelope in flow:
        started = time.monotonic()
        await worker.send_json(envelope)
        while True:
            message = await worker.receive_json(timeout=10)
            if message.get("type") != "ack":
                continue
            if int(message["last_seq"]) >= int(envelope["seq"]):
                break
        latencies.append(time.monotonic() - started)
    latencies.sort()
    assert latencies[-1] < 2.0
    await worker.close()


async def test_cross_replica_wake_where_possible(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    import os

    if not os.environ.get("APIPI_TEST_DATABASE_URL"):
        settings_obj = _worker_settings(settings)
        assert settings_obj.event_bus == "auto"
        return
    first_app = create_app(
        api_settings_for(_worker_settings(settings), batch_window_zero=False),
        store=store,
    )
    second_app = create_app(
        api_settings_for(_worker_settings(settings), batch_window_zero=False),
        store=store,
    )
    token = "t"
    tenant_id, session_id = await _session(first_app, token, store)
    worker = FakeWorker(first_app, worker_secret)
    hello = await worker.connect()
    await _lease(store, tenant_id, session_id, uuid.UUID(str(hello["worker_id"])))
    await second_app.state.event_hub.start()
    queue = second_app.state.event_hub.subscribe(session_id)
    try:
        started = time.monotonic()
        await worker.send_json(
            _envelope(session_id, 1, "session.status", {"status": "in_progress"})
        )
        await worker.send_json(
            _envelope(
                session_id,
                2,
                "event",
                {"type": "agent.session.in_progress", "data": {}},
            )
        )
        message = await asyncio.wait_for(queue.get(), timeout=10)
        elapsed = time.monotonic() - started
        assert message.get("kind") == "wake"
        assert elapsed < 2.0
    finally:
        second_app.state.event_hub.unsubscribe(session_id, queue)
        await second_app.state.event_hub.close()
        await worker.close()
