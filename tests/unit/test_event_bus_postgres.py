"""Postgres LISTEN/NOTIFY integration tests for the event bus.

How to run them: ``tests/support/postgres.py``.
"""

import asyncio
import uuid

from tests.support.postgres import needs_postgres, pg_dsn, postgres_replicas
from tests.support.waits import until

from apipi.common.event_bus import is_wake, message_seq
from apipi.common.metrics import Metrics
from apipi.services.event_bus import PostgresEventBus

pytestmark = needs_postgres


async def test_postgres_listener_reconnect_covers_gap() -> None:
    metrics = Metrics()
    async with postgres_replicas(metrics) as (replica_a, replica_b):
        session_id = uuid.uuid4()
        queue_b = replica_b.subscribe(session_id)
        try:
            listen = replica_b._listen
            assert listen is not None
            await listen.close()
            await until(
                lambda: (
                    replica_b._listen is not None and not replica_b._listen.is_closed()
                ),
                timeout=10.0,
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


async def test_postgres_live_batches_split_and_arrive() -> None:
    async with postgres_replicas() as (replica_a, replica_b):
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


async def test_postgres_no_self_delivery_duplicates() -> None:
    async with postgres_replicas() as (replica_a, replica_b):
        session_id = uuid.uuid4()
        queue_a = replica_a.subscribe(session_id)
        queue_b = replica_b.subscribe(session_id)
        delta = {
            "type": "agent.session.turn.output_text.delta",
            "session_id": str(session_id),
            "data": {"delta": "hi"},
        }
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
                await replica_a.publish(session_id, delta)
            async with asyncio.timeout(10):
                wake_b = await queue_b.get()
                live_b = [await queue_b.get() for _ in range(5)]
            assert is_wake(wake_b) and wake_b["seq"] == 5
            assert live_b == [delta] * 5
            await replica_b.publish(
                session_id,
                {
                    "id": "e2",
                    "type": "agent.session.idle",
                    "seq": 6,
                    "session_id": str(session_id),
                    "data": {},
                },
            )
            rest_a = []
            async with asyncio.timeout(10):
                while message_seq(item := await queue_a.get()) != 6:
                    rest_a.append(item)
            assert rest_a == [delta] * 5
        finally:
            replica_a.unsubscribe(session_id, queue_a)
            replica_b.unsubscribe(session_id, queue_b)


async def test_postgres_queue_usage_sample() -> None:
    replica = PostgresEventBus(pg_dsn(), metrics=Metrics())
    await replica.start()
    try:
        assert replica._listen is not None
        value = await replica._listen.fetchval("SELECT pg_notification_queue_usage()")
        assert isinstance(value, float) and 0.0 <= value <= 1.0
    finally:
        await replica.close()


async def test_postgres_instance_messages_reach_only_that_instance() -> None:
    async with postgres_replicas() as (replica_a, replica_b):
        got_a: list[dict[str, object]] = []
        got_b: list[dict[str, object]] = []
        await replica_a.listen_instance("node-a", got_a.append)
        await replica_b.listen_instance("node-b", got_b.append)
        await replica_a.send_instance("node-b", {"kind": "forward", "id": "x"})
        await until(lambda: bool(got_b), timeout=10.0)
        await asyncio.sleep(0.2)
        assert got_b == [{"kind": "forward", "id": "x"}]
        assert got_a == []
