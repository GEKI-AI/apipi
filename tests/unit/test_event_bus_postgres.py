"""Postgres LISTEN/NOTIFY integration tests for the event bus.

These need a live, migrated Postgres
(``apipi migrate`` against ``APIPI_TEST_DATABASE_URL``) and are marked
slow, so GitHub CI skips them. Run locally with e.g.::

    export APIPI_TEST_DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
    uv run pytest -m slow tests/unit/test_event_bus_postgres.py
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from apipi.common.event_bus import is_wake, message_seq
from apipi.common.metrics import Metrics
from apipi.services.event_bus import PostgresEventBus
from apipi.services.session_events import persist_event
from apipi.services.sessions import iter_session_events
from apipi.store.engine import Store
from apipi.store.repo import create_session, create_tenant

PG_URL = os.environ.get("APIPI_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not PG_URL, reason="needs APIPI_TEST_DATABASE_URL"),
]


def _dsn() -> str:
    assert PG_URL is not None
    return PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def _bus(metrics: Metrics | None = None) -> PostgresEventBus:
    return PostgresEventBus(_dsn(), metrics=metrics)


async def _wait_for(condition, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


async def test_postgres_fanout_two_replicas() -> None:
    replica_a = _bus()
    replica_b = _bus()
    await replica_a.start()
    await replica_b.start()
    try:
        session_id = uuid.uuid4()
        queue_b = replica_b.subscribe(session_id)
        try:
            await replica_a.publish(
                session_id,
                {
                    "id": "e1",
                    "type": "agent.session.turn.completed",
                    "seq": 41,
                    "session_id": str(session_id),
                    "data": {},
                },
            )
            wake = await asyncio.wait_for(queue_b.get(), timeout=10)
            assert is_wake(wake)
            assert wake["session_id"] == str(session_id)
            assert wake["seq"] == 41
        finally:
            replica_b.unsubscribe(session_id, queue_b)
    finally:
        await replica_a.close()
        await replica_b.close()


async def test_postgres_listener_reconnect_covers_gap() -> None:
    metrics = Metrics()
    replica_a = _bus()
    replica_b = _bus(metrics)
    await replica_a.start()
    await replica_b.start()
    try:
        session_id = uuid.uuid4()
        queue_b = replica_b.subscribe(session_id)
        try:
            listen = replica_b._listen
            assert listen is not None
            await listen.close()
            await _wait_for(
                lambda: (
                    replica_b._listen is not None and not replica_b._listen.is_closed()
                )
            )
            reconnects = metrics.registry.get_sample_value(
                "apipi_event_bus_listener_reconnects_total"
            )
            assert reconnects is not None and reconnects >= 1.0
            await replica_a.publish(
                session_id,
                {
                    "id": "e1",
                    "type": "agent.session.idle",
                    "seq": 3,
                    "session_id": str(session_id),
                    "data": {},
                },
            )
            wake = await asyncio.wait_for(queue_b.get(), timeout=10)
            assert is_wake(wake) and wake["seq"] == 3
        finally:
            replica_b.unsubscribe(session_id, queue_b)
    finally:
        await replica_a.close()
        await replica_b.close()


async def test_postgres_live_batches_split_and_arrive() -> None:
    replica_a = _bus()
    replica_b = _bus()
    await replica_a.start()
    await replica_b.start()
    try:
        session_id = uuid.uuid4()
        queue_b = replica_b.subscribe(session_id)
        deltas = [f"delta-{n}-" + ("x" * 200) for n in range(60)]
        try:
            for delta in deltas:
                await replica_a.publish(
                    session_id,
                    {
                        "type": "agent.session.turn.output_text.delta",
                        "session_id": str(session_id),
                        "data": {"delta": delta},
                    },
                )
            received: list[str] = []
            async with asyncio.timeout(15):
                while len(received) < len(deltas):
                    item = await queue_b.get()
                    received.append(item["data"]["delta"])
            assert sorted(received) == sorted(deltas)
        finally:
            replica_b.unsubscribe(session_id, queue_b)
    finally:
        await replica_a.close()
        await replica_b.close()


async def test_postgres_no_self_delivery_duplicates() -> None:
    replica_a = _bus()
    replica_b = _bus()
    await replica_a.start()
    await replica_b.start()
    try:
        session_id = uuid.uuid4()
        queue_a = replica_a.subscribe(session_id)
        queue_b = replica_b.subscribe(session_id)
        try:
            await replica_a.publish(
                session_id,
                {
                    "id": "e1",
                    "type": "agent.session.turn.completed",
                    "seq": 5,
                    "session_id": str(session_id),
                    "data": {},
                },
            )
            first = await asyncio.wait_for(queue_a.get(), timeout=10)
            assert message_seq(first) == 5
            for _ in range(5):
                await replica_a.publish(
                    session_id,
                    {
                        "type": "agent.session.turn.output_text.delta",
                        "session_id": str(session_id),
                        "data": {"delta": "hi"},
                    },
                )
            await asyncio.sleep(1.5)
            rest_a = []
            while not queue_a.empty():
                rest_a.append(queue_a.get_nowait())
            assert len(rest_a) == 5
            assert all("seq" not in item for item in rest_a)
            wake_b = await asyncio.wait_for(queue_b.get(), timeout=10)
            assert is_wake(wake_b) and wake_b["seq"] == 5
        finally:
            replica_a.unsubscribe(session_id, queue_a)
            replica_b.unsubscribe(session_id, queue_b)
    finally:
        await replica_a.close()
        await replica_b.close()
    replica = _bus(Metrics())
    await replica.start()
    try:
        assert replica._listen is not None
        value = await replica._listen.fetchval("SELECT pg_notification_queue_usage()")
        assert isinstance(value, float) and 0.0 <= value <= 1.0
    finally:
        await replica.close()


async def test_postgres_queue_usage_sample() -> None:
    replica = _bus(Metrics())
    await replica.start()
    try:
        assert replica._listen is not None
        value = await replica._listen.fetchval("SELECT pg_notification_queue_usage()")
        assert isinstance(value, float) and 0.0 <= value <= 1.0
    finally:
        await replica.close()


async def test_postgres_sse_across_replicas_without_polling() -> None:
    assert PG_URL is not None
    engine_a = create_async_engine(PG_URL, pool_pre_ping=True)
    engine_b = create_async_engine(PG_URL, pool_pre_ping=True)
    store_a = Store(engine_a)
    store_b = Store(engine_b)
    replica_a = _bus()
    replica_b = _bus()
    await replica_a.start()
    await replica_b.start()
    try:
        async with store_a.session() as db:
            tenant = await create_tenant(db, name="bus-e2e")
            session_row = await create_session(db, tenant.id)
            tenant_id = tenant.id
            session_id = session_row.id
        received: list[dict] = []  # type: ignore[type-arg]
        started = asyncio.Event()

        async def consume() -> None:
            agen = iter_session_events(
                store_b,
                replica_b,
                tenant_id,
                session_id,
                None,
                fallback_poll=30.0,
            )
            try:
                async for item in agen:
                    if item is None:
                        continue
                    received.append(item)
                    started.set()
                    return
            finally:
                await agen.aclose()

        task = asyncio.create_task(consume())
        try:
            await _wait_for(lambda: session_id in replica_b._subs)
            async with store_a.session() as db:
                event = await persist_event(
                    db,
                    replica_a,
                    tenant_id,
                    session_id,
                    type="agent.session.turn.completed",
                    data={"status": "completed"},
                )
                assert event is not None
            await asyncio.wait_for(started.wait(), timeout=15)
            assert [item["type"] for item in received] == [
                "agent.session.turn.completed"
            ]
            assert received[0]["seq"] == event.seq
        finally:
            if not task.done():
                task.cancel()
    finally:
        await replica_a.close()
        await replica_b.close()
        await store_a.dispose()
        await store_b.dispose()
