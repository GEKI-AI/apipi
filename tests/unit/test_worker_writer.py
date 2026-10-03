import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.websockets import WebSocketState

from apipi.workerhub.connection import WorkerConnection
from apipi.workerhub.writer import ConnectionWriter, WorkerSendError


class _Socket(SimpleNamespace):
    def __init__(self, delay: float = 0.0, *, block: bool = False) -> None:
        super().__init__(client_state=WebSocketState.CONNECTED, sent=[])
        self.delay = delay
        self.block = block

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.block:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(payload)


def _conn(socket: _Socket) -> WorkerConnection:
    return WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=cast(Any, socket),
        capacity=1,
        memory_mb=512,
        run_mode="none",
    )


async def test_frames_are_written_in_order_by_one_task() -> None:
    socket = _Socket(delay=0.001)
    conn = _conn(socket)
    conn.writer.start()
    try:
        await asyncio.gather(*(conn.send({"n": n}) for n in range(20)))
        assert [item["n"] for item in socket.sent] == list(range(20))
    finally:
        await conn.writer.stop()


async def test_unstarted_writer_writes_inline() -> None:
    socket = _Socket()
    conn = _conn(socket)
    await conn.send({"n": 1})
    assert socket.sent == [{"n": 1}]


async def test_write_timeout_closes_the_connection_and_fails_pending_sends() -> None:
    socket = _Socket(block=True)
    conn = _conn(socket)
    conn.writer.timeout = 0.05
    depth: list[int] = []
    conn.writer.bind(None, lambda: depth.append(conn.writer.depth))
    conn.writer.start()
    first = asyncio.create_task(conn.send({"n": 1}))
    second = asyncio.create_task(conn.send({"n": 2}))
    await asyncio.wait_for(conn.closing.wait(), timeout=2)
    assert conn.disconnect_reason == "write_timeout"
    assert conn.close_code == 1011
    for task in (first, second):
        with pytest.raises(WorkerSendError):
            await task
    assert max(depth) >= 1
    assert conn.writer.depth == 0
    with pytest.raises(WorkerSendError):
        await conn.send({"n": 3})
    await conn.writer.stop()


async def test_a_full_queue_closes_the_connection() -> None:
    socket = _Socket(block=True)
    conn = _conn(socket)
    conn.writer = ConnectionWriter(conn, limit=2, timeout=30)
    conn.writer.start()
    pending = [asyncio.create_task(conn.send({"n": n})) for n in range(2)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    pending.append(asyncio.create_task(conn.send({"n": 2})))
    pending.append(asyncio.create_task(conn.send({"n": 3})))
    await asyncio.wait_for(conn.closing.wait(), timeout=2)
    assert conn.disconnect_reason == "write_timeout"
    await conn.writer.stop()
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert all(isinstance(item, WorkerSendError) for item in results)


async def test_a_socket_error_fails_the_send_and_later_sends() -> None:
    class Broken(_Socket):
        async def send_json(self, payload: dict[str, Any]) -> None:
            raise RuntimeError("closed")

    conn = _conn(Broken())
    conn.writer.start()
    with pytest.raises(RuntimeError):
        await conn.send({"n": 1})
    with pytest.raises(WorkerSendError):
        await conn.send({"n": 2})
    await conn.writer.stop()
