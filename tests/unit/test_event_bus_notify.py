import asyncio
import logging
import uuid
from typing import cast

import asyncpg
import pytest

from apipi.common.metrics import Metrics
from apipi.services.event_bus import PostgresEventBus


class _PublishConn:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.running = 0
        self.overlap = 0
        self.calls = 0
        self.fail = fail
        self.terminated = False

    def is_closed(self) -> bool:
        return self.terminated

    def terminate(self) -> None:
        self.terminated = True

    async def execute(self, *_args: object) -> None:
        self.calls += 1
        if self.running:
            self.overlap += 1
            raise asyncpg.InterfaceError(
                "cannot perform operation: another operation is in progress"
            )
        self.running += 1
        try:
            await asyncio.sleep(0.005)
            if self.fail is not None:
                raise self.fail
        finally:
            self.running -= 1


def _bus_with(conn: _PublishConn, metrics: Metrics | None = None) -> PostgresEventBus:
    bus = PostgresEventBus("postgresql://example/db", metrics=metrics)
    bus._running = True
    bus._publish_conn = cast(asyncpg.Connection, conn)
    return bus


async def test_notify_is_serialized_across_concurrent_publishes() -> None:
    conn = _PublishConn()
    metrics = Metrics()
    bus = _bus_with(conn, metrics)
    session_id = uuid.uuid4()
    await asyncio.gather(
        *(
            bus.publish(session_id, {"type": "e", "seq": seq, "session_id": "s"})
            for seq in range(1, 21)
        ),
        *(
            bus.publish(session_id, {"type": "agent.delta", "data": {"n": n}})
            for n in range(5)
        ),
        bus._flush_live_once(),
    )
    await bus._flush_live_once()
    assert conn.calls >= 20
    assert conn.overlap == 0
    assert "apipi_event_bus_notify_errors_total 0.0" in metrics.scrape().decode()


@pytest.mark.parametrize(
    "error",
    [asyncpg.InterfaceError("closed"), RuntimeError("boom"), ConnectionResetError()],
)
async def test_notify_errors_are_caught_counted_and_drop_the_connection(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    conn = _PublishConn(fail=error)
    metrics = Metrics()
    bus = _bus_with(conn, metrics)
    session_id = uuid.uuid4()
    queue = bus.subscribe(session_id)
    body = {"type": "e", "seq": 1, "session_id": str(session_id)}
    await bus.publish(session_id, body)
    assert queue.get_nowait() == body
    assert conn.terminated is True
    assert bus._publish_conn is None
    assert "apipi_event_bus_notify_errors_total 1.0" in metrics.scrape().decode()
    assert any(
        getattr(record, "event", None) == "event_bus.notify.failed"
        for record in caplog.records
    )


async def test_live_flush_survives_notify_errors() -> None:
    conn = _PublishConn(fail=asyncpg.InterfaceError("closed"))
    metrics = Metrics()
    bus = _bus_with(conn, metrics)
    session_id = uuid.uuid4()
    await bus.publish(session_id, {"type": "agent.delta", "data": {"n": 1}})
    await bus._flush_live_once()
    assert "apipi_event_bus_notify_errors_total 1.0" in metrics.scrape().decode()
