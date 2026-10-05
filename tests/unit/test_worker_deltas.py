import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast

import pytest
from tests.support.postgres import needs_postgres, postgres_replicas

from apipi.common.event_bus import EventBus, InMemoryEventBus, live_event_body
from apipi.common.ratelimit import relay_rate_allowed
from apipi.config import Settings
from apipi.protocol import DELTA_MAX_TEXT, WorkerEnvelope
from apipi.store.engine import Store
from apipi.store.repo import (
    append_event,
    clear_session_lease,
    create_session,
    create_tenant,
    set_session_lease,
)
from apipi.worker.deltas import DeltaRelay, LiveRedirectBus
from apipi.workerhub.connection import WorkerConnection
from apipi.workerhub.hub import WorkerHub


def _envelope(
    session_id: uuid.UUID, turn_id: uuid.UUID, text: str, seq: int = 1
) -> WorkerEnvelope:
    return WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "turn_id": str(turn_id),
            "seq": seq,
            "type": "delta.text",
            "payload": {"turn_id": str(turn_id), "text": text},
        }
    )


async def test_relay_coalesces_fragments_into_one_envelope() -> None:
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=60.0)
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    await relay.submit(session_id, turn_id, "hel")
    await relay.submit(session_id, turn_id, "lo ")
    await relay.submit(session_id, turn_id, "world")
    await relay.flush()
    assert len(sent) == 1
    envelope = sent[0]
    assert envelope["type"] == "delta.text"
    assert envelope["payload"] == {"turn_id": str(turn_id), "text": "hello world"}
    assert envelope["session_id"] == str(session_id)
    assert envelope["seq"] == 1


async def test_relay_splits_large_buffers_and_keeps_seq() -> None:
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=60.0, max_text=4)
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    await relay.submit(session_id, turn_id, "abcdefgh")
    await relay.flush()
    assert [item["payload"]["text"] for item in sent] == ["abcd", "efgh"]
    assert [item["seq"] for item in sent] == [1, 2]


async def test_relay_drops_empty_fragments() -> None:
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=60.0)
    await relay.submit(uuid.uuid4(), uuid.uuid4(), "")
    await relay.flush()
    assert sent == []


async def test_relay_send_failure_is_dropped_not_raised() -> None:
    async def boom(envelope: dict[str, Any]) -> None:
        del envelope
        raise RuntimeError("socket closed")

    relay = DeltaRelay(boom, window=60.0)
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    await relay.submit(session_id, turn_id, "hi")
    await relay.flush()
    assert relay.dropped == 1


async def test_relay_detach_drops_quietly() -> None:
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=60.0)
    relay.detach()
    await relay.submit(uuid.uuid4(), uuid.uuid4(), "hi")
    await relay.flush()
    assert sent == []
    assert relay.dropped == 1


async def test_relay_forget_resets_session() -> None:
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=60.0)
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    await relay.submit(session_id, turn_id, "a")
    await relay.submit(session_id, turn_id, "b")
    await relay.flush()
    assert [item["seq"] for item in sent] == [1]
    relay.forget(session_id)
    await relay.submit(session_id, turn_id, "c")
    await relay.flush()
    assert [item["seq"] for item in sent] == [1, 1]
    assert sent[1]["payload"] == {"turn_id": str(turn_id), "text": "c"}


async def test_relay_window_flushes_without_caller() -> None:
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=0.01)
    await relay.submit(uuid.uuid4(), uuid.uuid4(), "hi")
    await asyncio.sleep(0.05)
    assert len(sent) == 1


def test_rate_limiter_allows_burst_then_rejects() -> None:
    hits: list[float] = []
    assert relay_rate_allowed(hits, now=100.0, limit=2)
    assert relay_rate_allowed(hits, now=100.1, limit=2)
    assert not relay_rate_allowed(hits, now=100.2, limit=2)
    assert relay_rate_allowed(hits, now=101.5, limit=2)


async def test_live_bus_redirects_deltas_to_relay() -> None:
    inner = InMemoryEventBus()
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=60.0)
    bus = LiveRedirectBus(inner, relay)
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    inner_queue = inner.subscribe(session_id)
    try:
        await bus.publish(
            session_id,
            live_event_body(
                session_id,
                type="agent.session.turn.output_text.delta",
                data={"delta": "hi", "turn_id": str(turn_id)},
            ),
        )
        await relay.flush()
        assert inner_queue.empty()
        assert len(sent) == 1
        await bus.publish(
            session_id,
            {
                "id": "e1",
                "type": "agent.session.turn.completed",
                "seq": 3,
                "session_id": str(session_id),
                "data": {},
            },
        )
        stored = await asyncio.wait_for(inner_queue.get(), timeout=2)
        assert stored["seq"] == 3
    finally:
        inner.unsubscribe(session_id, inner_queue)


async def test_live_bus_without_relay_publishes_directly() -> None:
    inner = InMemoryEventBus()
    bus = LiveRedirectBus(inner)
    session_id = uuid.uuid4()
    queue = inner.subscribe(session_id)
    try:
        body = live_event_body(
            session_id,
            type="agent.session.turn.output_text.delta",
            data={"delta": "hi"},
        )
        await bus.publish(session_id, body)
        assert await asyncio.wait_for(queue.get(), timeout=2) == body
    finally:
        inner.unsubscribe(session_id, queue)


def _conn(worker_id: uuid.UUID, lease_id: uuid.UUID) -> WorkerConnection:
    return WorkerConnection(
        worker_id=worker_id,
        generation=1,
        websocket=cast(Any, None),
        capacity=2,
        memory_mb=512,
        run_mode="none",
        leases={lease_id},
    )


async def _leased(
    store: Store, worker_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    from apipi.store.models import utc_now

    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        lease_id = uuid.uuid4()
        await set_session_lease(
            db,
            tenant.id,
            session_row.id,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + timedelta(minutes=5),
        )
        return tenant.id, session_row.id, lease_id, worker_id


@pytest.fixture(params=["memory", pytest.param("postgres", marks=needs_postgres)])
async def buses(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[EventBus, EventBus]]:
    if request.param == "memory":
        bus = InMemoryEventBus()
        yield bus, bus
        return
    async with postgres_replicas() as pair:
        yield pair


async def test_handle_delta_publishes_live_without_db_writes(
    settings: Settings, store: Store, buses: tuple[EventBus, EventBus]
) -> None:
    publish, subscribe = buses
    hub = WorkerHub(settings)
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id, _ = await _leased(store, worker_id)
    queue = subscribe.subscribe(session_id)
    try:
        turn_id = uuid.uuid4()
        published = await hub.handle_delta(
            store,
            publish,
            _conn(worker_id, lease_id),
            _envelope(session_id, turn_id, "hi"),
        )
        assert published is True
        message = await asyncio.wait_for(queue.get(), timeout=15)
        assert message["type"] == "agent.session.turn.output_text.delta"
        assert message["data"] == {"delta": "hi", "turn_id": str(turn_id)}
        assert "seq" not in message
        async with store.session() as db:
            from apipi.store.repo import list_events

            assert await list_events(db, tenant_id, session_id) == []
    finally:
        subscribe.unsubscribe(session_id, queue)


async def test_handle_delta_rejects_unleased_session(
    settings: Settings,
    store: Store,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    _, session_id, _, _ = await _leased(store, uuid.uuid4())
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    published = await hub.handle_delta(
        store,
        bus,
        _conn(worker_id, uuid.uuid4()),
        _envelope(session_id, uuid.uuid4(), "hi"),
    )
    assert published is False
    assert "worker delta for unleased session" in caplog.text


async def _noted(
    hub: WorkerHub, tenant_id: uuid.UUID, session_id: uuid.UUID, worker_id, lease_id
) -> None:
    hub._note_delta_lease(
        session_id, worker_id=worker_id, lease_id=lease_id, tenant_id=tenant_id
    )


@pytest.mark.parametrize(
    ("stored", "same_turn", "published"),
    [
        ("agent.session.turn.output_text.done", True, False),
        ("agent.session.turn.completed", True, False),
        ("agent.session.turn.output_text.done", False, True),
    ],
    ids=["after_done", "after_terminal_turn", "other_turn"],
)
async def test_handle_delta_drops_only_deltas_of_ended_turns(
    settings: Settings, store: Store, stored: str, same_turn: bool, published: bool
) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id, _ = await _leased(store, worker_id)
    turn_id = uuid.uuid4()
    await _noted(hub, tenant_id, session_id, worker_id, lease_id)
    ended = turn_id if same_turn else uuid.uuid4()
    hub.note_stored_events(
        session_id, [{"type": stored, "data": {"turn_id": str(ended)}}]
    )
    queue = bus.subscribe(session_id)
    try:
        assert (
            await hub.handle_delta(
                store,
                bus,
                _conn(worker_id, lease_id),
                _envelope(session_id, turn_id, "late"),
            )
            is published
        )
        sent = [queue.get_nowait()["data"]["delta"] for _ in range(queue.qsize())]
        assert sent == (["late"] if published else [])
    finally:
        bus.unsubscribe(session_id, queue)


async def test_handle_delta_rejects_oversize(settings: Settings, store: Store) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    _, session_id, lease_id, _ = await _leased(store, worker_id)
    published = await hub.handle_delta(
        store,
        bus,
        _conn(worker_id, lease_id),
        _envelope(session_id, uuid.uuid4(), "x" * (DELTA_MAX_TEXT + 1)),
    )
    assert published is False


async def test_handle_delta_ignores_reasoning(settings: Settings, store: Store) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    _, session_id, lease_id, _ = await _leased(store, worker_id)
    turn_id = uuid.uuid4()
    envelope = WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "turn_id": str(turn_id),
            "seq": 1,
            "type": "delta.reasoning",
            "payload": {"turn_id": str(turn_id), "text": "hmm"},
        }
    )
    queue = bus.subscribe(session_id)
    try:
        assert (
            await hub.handle_delta(store, bus, _conn(worker_id, lease_id), envelope)
            is False
        )
        assert queue.empty()
    finally:
        bus.unsubscribe(session_id, queue)


async def test_handle_delta_rate_limits(settings: Settings, store: Store) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    _, session_id, lease_id, _ = await _leased(store, worker_id)
    conn = _conn(worker_id, lease_id)
    turn_id = uuid.uuid4()
    accepted = 0
    for seq in range(1, 150):
        if await hub.handle_delta(
            store, bus, conn, _envelope(session_id, turn_id, "x", seq=seq)
        ):
            accepted += 1
    assert accepted == 100


async def test_handle_delta_counts_protocol_events(
    settings: Settings, store: Store
) -> None:
    from apipi.common.metrics import Metrics

    metered = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        metrics=True,
    )
    hub = WorkerHub(metered, metrics=Metrics())
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    _, session_id, lease_id, _ = await _leased(store, worker_id)
    assert await hub.handle_delta(
        store,
        bus,
        _conn(worker_id, lease_id),
        _envelope(session_id, uuid.uuid4(), "hi"),
    )
    assert (
        await hub.handle_delta(
            store,
            bus,
            _conn(worker_id, uuid.uuid4()),
            _envelope(uuid.uuid4(), uuid.uuid4(), "hi"),
        )
        is False
    )
    assert hub.metrics is not None
    body = hub.metrics.scrape().decode()
    assert 'event="delta.accepted"' in body
    assert 'event="delta.rejected"' in body


async def test_handle_delta_reads_no_events(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apipi.store.repo as repo

    calls = 0
    real = repo.list_events

    async def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(repo, "list_events", counting)
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id, _ = await _leased(store, worker_id)
    async with store.session() as db:
        for _ in range(30):
            await append_event(
                db,
                tenant_id,
                session_id,
                type="agent.session.turn.output_text.done",
                data={"text": "old", "turn_id": str(uuid.uuid4())},
            )
    conn = _conn(worker_id, lease_id)
    turn_id = uuid.uuid4()
    for seq in range(1, 6):
        assert await hub.handle_delta(
            store, bus, conn, _envelope(session_id, turn_id, "x", seq=seq)
        )
    assert calls == 0


async def test_handle_delta_uses_no_query_once_leased(
    settings: Settings, store: Store
) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id, _ = await _leased(store, worker_id)
    await _noted(hub, tenant_id, session_id, worker_id, lease_id)
    turn_id = uuid.uuid4()
    conn = _conn(worker_id, lease_id)

    class NoStore:
        def session(self) -> Any:
            raise AssertionError("a delta must not read the database")

    assert await hub.handle_delta(
        cast(Any, NoStore()), bus, conn, _envelope(session_id, turn_id, "x")
    )
    hub.note_stored_events(
        session_id,
        [
            {
                "type": "agent.session.turn.output_text.done",
                "data": {"text": "full", "turn_id": str(turn_id)},
            }
        ],
    )
    assert (
        await hub.handle_delta(
            cast(Any, NoStore()),
            bus,
            conn,
            _envelope(session_id, turn_id, "late", seq=2),
        )
        is False
    )


async def test_delta_state_dropped_on_release_and_detach(
    settings: Settings, store: Store
) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id, _ = await _leased(store, worker_id)
    conn = _conn(worker_id, lease_id)
    await hub.attach(conn)
    try:
        turn_id = uuid.uuid4()
        assert await hub.handle_delta(
            store, bus, conn, _envelope(session_id, turn_id, "hi")
        )
        assert session_id in hub._delta_leases
        assert session_id in hub._delta_hits
        await hub.release(store, tenant_id, session_id, lease_id)
        assert session_id not in hub._delta_leases
        assert session_id not in hub._delta_hits
        lease_id2 = uuid.uuid4()
        async with store.session() as db:
            from apipi.store.models import utc_now

            await set_session_lease(
                db,
                tenant_id,
                session_id,
                worker_id=worker_id,
                lease_id=lease_id2,
                lease_until=utc_now() + timedelta(minutes=5),
            )
        conn.leases.add(lease_id2)
        assert await hub.handle_delta(
            store, bus, conn, _envelope(session_id, uuid.uuid4(), "again")
        )
        assert session_id in hub._delta_leases
    finally:
        await hub.detach(worker_id, conn)
    assert session_id not in hub._delta_leases
    assert session_id not in hub._delta_hits


async def test_stale_delta_lease_is_revalidated(
    settings: Settings, store: Store
) -> None:
    hub = WorkerHub(settings)
    bus = InMemoryEventBus()
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id, _ = await _leased(store, worker_id)
    conn = _conn(worker_id, lease_id)
    turn_id = uuid.uuid4()
    assert await hub.handle_delta(
        store, bus, conn, _envelope(session_id, turn_id, "hi")
    )
    async with store.session() as db:
        await clear_session_lease(db, tenant_id, session_id)
    hub._delta_leases[session_id].refreshed -= 3600.0
    assert (
        await hub.handle_delta(
            store, bus, conn, _envelope(session_id, turn_id, "late", seq=2)
        )
        is False
    )
    assert session_id not in hub._delta_leases
