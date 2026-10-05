import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest
from tests.support.notify_bus import NotifyNetwork
from tests.support.postgres import needs_postgres, postgres_replicas

from apipi.common.event_bus import (
    EventHub,
    InMemoryEventBus,
    is_wake,
    message_seq,
    wake_message,
)
from apipi.common.metrics import Metrics
from apipi.config import ConfigError, Settings
from apipi.services.event_bus import (
    PostgresEventBus,
    create_event_bus,
    resolve_event_bus_name,
    split_notify_batches,
)
from apipi.services.session_events import persist_event
from apipi.services.sessions import iter_session_events
from apipi.store import events as store_events
from apipi.store.engine import Store
from apipi.store.repo import create_session, create_tenant
from apipi.worker.execution import local_execution
from apipi.worker.fake_harness import FakeHarness
from apipi.worker.outbox import Outbox
from apipi.workerhub.execution import RemoteExecution

Replica = InMemoryEventBus | PostgresEventBus


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.wait({task}, timeout=5)


def test_event_hub_is_the_memory_bus() -> None:
    assert EventHub is InMemoryEventBus


def test_wake_helpers() -> None:
    session_id = uuid.uuid4()
    wake = wake_message(session_id, 7)
    assert is_wake(wake)
    assert message_seq(wake) == 7
    assert wake["session_id"] == str(session_id)
    assert isinstance(wake["published_at"], float)
    assert not is_wake({"type": "x", "seq": 1})
    assert message_seq({"type": "x", "seq": 3}) == 3
    assert message_seq({"type": "live"}) is None


@pytest.fixture(params=["memory", "postgres_down"])
async def local_bus(request: pytest.FixtureRequest) -> AsyncIterator[Replica]:
    if request.param == "memory":
        yield InMemoryEventBus()
        return

    async def _close(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        del reader
        writer.close()

    server = await asyncio.start_server(_close, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]
    bus = PostgresEventBus(f"postgresql://127.0.0.1:{port}/db")
    try:
        yield bus
    finally:
        await bus.close()
        server.close()
        await server.wait_closed()


async def test_bus_fans_out_to_local_subscribers(local_bus: Replica) -> None:
    session_id = uuid.uuid4()
    first = local_bus.subscribe(session_id)
    second = local_bus.subscribe(session_id)
    other = local_bus.subscribe(uuid.uuid4())
    body = {"type": "t", "seq": 1}
    await local_bus.publish(session_id, body)
    assert await asyncio.wait_for(first.get(), timeout=2) == body
    assert await asyncio.wait_for(second.get(), timeout=2) == body
    assert other.empty()
    local_bus.unsubscribe(session_id, first)
    await local_bus.publish(session_id, body)
    assert first.empty()
    assert await asyncio.wait_for(second.get(), timeout=2) == body
    local_bus.unsubscribe(session_id, second)


async def test_memory_bus_lifecycle_is_noop() -> None:
    bus = InMemoryEventBus()
    await bus.start()
    await bus.close()


async def test_persist_event_publishes_after_commit(store: Store) -> None:
    bus = InMemoryEventBus()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
    queue = bus.subscribe(session_id)
    try:
        async with store.session() as db:
            event = await persist_event(
                db,
                bus,
                tenant_id,
                session_id,
                type="agent.session.turn.output_text.done",
                data={"text": "hi"},
            )
            assert event is not None
            assert queue.empty()
        message = await asyncio.wait_for(queue.get(), timeout=2)
        assert message["seq"] == event.seq == 1
        assert message["type"] == "agent.session.turn.output_text.done"
    finally:
        bus.unsubscribe(session_id, queue)


async def test_persist_event_live_publishes_immediately(store: Store) -> None:
    bus = InMemoryEventBus()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
    queue = bus.subscribe(session_id)
    try:
        async with store.session() as db:
            live = await persist_event(
                db,
                bus,
                tenant_id,
                session_id,
                type="agent.session.turn.output_text.delta",
                data={"delta": "hi"},
            )
            assert live is None
            assert not queue.empty()
    finally:
        bus.unsubscribe(session_id, queue)


async def test_failed_commit_drops_the_wake(store: Store) -> None:
    bus = InMemoryEventBus()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
    queue = bus.subscribe(session_id)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            async with store.session() as db:
                await persist_event(
                    db, bus, tenant_id, session_id, type="agent.session.idle"
                )
                raise RuntimeError("boom")
        await asyncio.sleep(0.05)
        assert queue.empty()
    finally:
        bus.unsubscribe(session_id, queue)


def test_split_notify_batches() -> None:
    small = [{"type": "d", "data": {"delta": "x"}} for _ in range(10)]
    batches = split_notify_batches(small)
    assert batches == [small]
    assert len(json.dumps(batches[0]).encode()) <= 8070
    big = [{"type": "d", "data": {"delta": "y" * 500}} for _ in range(40)]
    batches = split_notify_batches(big)
    assert len(batches) > 1
    assert sum(len(batch) for batch in batches) == len(big)
    for batch in batches:
        assert len(json.dumps(batch, separators=(",", ":")).encode()) <= 8000
    oversize = [{"type": "d", "data": {"delta": "z" * 9000}}]
    assert split_notify_batches(oversize) == [oversize]


def test_event_bus_follows_the_database_url(tmp_path: Path) -> None:
    sqlite = "sqlite+aiosqlite:///:memory:"
    postgres = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
    for url, event_bus, name, bus in (
        (sqlite, None, "memory", InMemoryEventBus),
        (postgres, None, "postgres", PostgresEventBus),
        (postgres, "memory", "memory", InMemoryEventBus),
        (sqlite, "postgres", "postgres", None),
    ):
        fields: dict[str, Any] = {
            "database_url": url,
            "run_mode": "none",
            "sessions_dir": str(tmp_path / "sessions"),
        }
        if event_bus is not None:
            fields["event_bus"] = event_bus
        settings = Settings(**fields)
        assert resolve_event_bus_name(settings) == name
        assert settings.event_bus_fallback_poll.total_seconds() == 3.0
        if bus is None:
            with pytest.raises(ConfigError, match="Postgres DATABASE_URL"):
                create_event_bus(settings)
        else:
            assert isinstance(create_event_bus(settings, metrics=Metrics()), bus)
        execution = local_execution(settings, outbox=Outbox(), harness=FakeHarness())
        assert isinstance(execution.hub, InMemoryEventBus)


async def test_factory_matches_injected_store(store: Store, tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )
    expected = (
        InMemoryEventBus if store.engine.dialect.name == "sqlite" else PostgresEventBus
    )
    assert isinstance(create_event_bus(settings, store=store), expected)
    assert resolve_event_bus_name(settings, engine_url="sqlite://") == "memory"
    assert resolve_event_bus_name(settings, engine_url="postgresql://x") == "postgres"


def test_injected_postgres_store_keeps_the_password_in_the_bus_dsn(
    tmp_path: Path,
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("postgresql+asyncpg://apipi:s3cret@db.example/apipi")
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:s3cret@db.example/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )
    bus = create_event_bus(settings, store=Store(engine))
    assert isinstance(bus, PostgresEventBus)
    assert "s3cret" in bus._dsn
    assert "***" not in bus._dsn


def test_unknown_event_bus_fails(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="event_bus"):
        Settings.model_validate(
            {
                "database_url": "sqlite+aiosqlite:///:memory:",
                "run_mode": "none",
                "sessions_dir": str(tmp_path / "sessions"),
                "event_bus": "nats",
            }
        )


def _conn() -> asyncpg.Connection:
    return cast("asyncpg.Connection", None)


def test_postgres_on_notify_dispatches_without_server() -> None:
    bus = PostgresEventBus("postgresql://127.0.0.1:1/db")
    session_id = uuid.uuid4()
    queue = bus.subscribe(session_id)
    try:
        bus._on_notify(_conn(), 0, "apipi_events", "not json")
        assert queue.empty()
        bus._on_notify(_conn(), 0, "apipi_events", json.dumps([1, 2]))
        assert queue.empty()
        bus._on_notify(
            _conn(),
            0,
            "apipi_events",
            json.dumps(wake_message(session_id, 9)),
        )
        wake = queue.get_nowait()
        assert is_wake(wake) and wake["seq"] == 9
        bus._on_notify(
            _conn(),
            0,
            "apipi_events",
            json.dumps(
                {
                    "kind": "live",
                    "session_id": str(session_id),
                    "batch": [{"type": "d", "data": {}}],
                }
            ),
        )
        assert queue.get_nowait() == {"type": "d", "data": {}}
        own_wake = wake_message(session_id, 10)
        own_wake["origin"] = bus._origin
        bus._on_notify(_conn(), 0, "apipi_events", json.dumps(own_wake))
        assert queue.empty()
        own_live = {
            "kind": "live",
            "session_id": str(session_id),
            "batch": [{"type": "d", "data": {}}],
            "origin": bus._origin,
        }
        bus._on_notify(_conn(), 0, "apipi_events", json.dumps(own_live))
        assert queue.empty()
        foreign_wake = wake_message(session_id, 11)
        foreign_wake["origin"] = "other-replica"
        bus._on_notify(_conn(), 0, "apipi_events", json.dumps(foreign_wake))
        assert queue.get_nowait()["seq"] == 11
    finally:
        bus.unsubscribe(session_id, queue)


async def test_sse_idle_stream_queries_only_on_fallback(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = InMemoryEventBus()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
    calls = 0

    async def _counting(db, tenant_id, session_id, *, after_seq=None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return await store_events.list_events(
            db, tenant_id, session_id, after_seq=after_seq
        )

    monkeypatch.setattr("apipi.services.sessions.list_events", _counting)
    agen = iter_session_events(
        store, bus, tenant_id, session_id, None, fallback_poll=0.05
    )

    async def _drain() -> None:
        try:
            async for _ in agen:
                pass
        finally:
            await agen.aclose()

    drainer = asyncio.create_task(_drain())
    try:
        await asyncio.sleep(0.22)
        assert 1 <= calls <= 6
    finally:
        await _stop(drainer)


@pytest.fixture(params=["notify", pytest.param("postgres", marks=needs_postgres)])
async def replicas(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[Replica, Replica]]:
    if request.param == "notify":
        network = NotifyNetwork()
        yield network.bus(), network.bus()
        return
    async with postgres_replicas() as pair:
        yield pair


async def test_sse_wake_from_another_replica_replays_without_polling(
    store: Store, replicas: tuple[Replica, Replica]
) -> None:
    replica_a, replica_b = replicas
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
    received: list[dict] = []  # type: ignore[type-arg]

    async def consume() -> None:
        agen = iter_session_events(
            store, replica_b, tenant_id, session_id, None, fallback_poll=30.0
        )
        try:
            async for item in agen:
                if item is None:
                    continue
                received.append(item)
                return
        finally:
            await agen.aclose()

    task = asyncio.create_task(consume())
    try:
        for _ in range(100):
            if session_id in replica_b._subs:
                break
            await asyncio.sleep(0.01)
        async with store.session() as db:
            event = await persist_event(
                db,
                replica_a,
                tenant_id,
                session_id,
                type="agent.session.turn.completed",
                data={"status": "completed"},
            )
            assert event is not None
        await asyncio.wait_for(task, timeout=15)
        assert [item["type"] for item in received] == ["agent.session.turn.completed"]
        assert received[0]["seq"] == event.seq
    finally:
        await _stop(task)


@pytest.mark.parametrize(
    "terminal", ["agent.session.turn.completed", "agent.session.turn.failed"]
)
async def test_remote_wait_ignores_live_and_returns_on_terminal(
    store: Store, settings: Settings, terminal: str
) -> None:
    bus = InMemoryEventBus()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
    execution = RemoteExecution(settings, workers=object(), store=store, hub=bus)
    waiter = asyncio.create_task(execution._wait(tenant_id, session_id))
    try:
        await asyncio.sleep(0.05)
        await bus.publish(
            session_id,
            {
                "type": "agent.session.turn.output_text.delta",
                "session_id": str(session_id),
                "data": {"delta": "hi"},
            },
        )
        await asyncio.sleep(0.1)
        assert not waiter.done()
        async with store.session() as db:
            await persist_event(db, bus, tenant_id, session_id, type=terminal)
            await persist_event(
                db, bus, tenant_id, session_id, type="agent.session.idle"
            )
        await asyncio.wait_for(waiter, timeout=5)
    finally:
        await _stop(waiter)


async def test_wake_sse_latency_observed(store: Store) -> None:
    bus = InMemoryEventBus()
    metrics = Metrics()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session_row = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session_row.id
        await persist_event(
            db, bus, tenant_id, session_id, type="agent.session.turn.completed"
        )
    received: list[dict] = []  # type: ignore[type-arg]

    async def consume() -> None:
        agen = iter_session_events(
            store,
            bus,
            tenant_id,
            session_id,
            1,
            fallback_poll=30.0,
            metrics=metrics,
        )
        try:
            async for item in agen:
                if item is None:
                    continue
                received.append(item)
                return
        finally:
            await agen.aclose()

    task = asyncio.create_task(consume())
    try:
        for _ in range(100):
            if session_id in bus._subs:
                break
            await asyncio.sleep(0.01)
        async with store.session() as db:
            await store_events.append_event(
                db,
                tenant_id,
                session_id,
                type="agent.session.turn.failed",
            )
        wake = wake_message(session_id, 2)
        wake["published_at"] = time.time() - 0.05
        bus._dispatch(session_id, wake)
        await asyncio.wait_for(task, timeout=2)
        assert [item["seq"] for item in received] == [2]
        assert (
            metrics.registry.get_sample_value("apipi_event_bus_wake_sse_seconds_count")
            == 1.0
        )
    finally:
        await _stop(task)
