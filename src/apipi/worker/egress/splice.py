import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from apipi.worker.egress.sockets import CHUNK, close_writer

IDLE_TIMEOUT = 300.0


@dataclass
class ByteCount:
    up: int = 0
    down: int = 0
    active: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.active = time.monotonic()


async def watch_idle(
    count: ByteCount, timeout: float, on_idle: Callable[[], None]
) -> None:
    count.touch()
    while True:
        left = count.active + timeout - time.monotonic()
        if left <= 0:
            on_idle()
            return
        await asyncio.sleep(left)


async def _pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    count: ByteCount,
    direction: str,
) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            count.touch()
            writer.write(data)
            if direction == "up":
                count.up += len(data)
            else:
                count.down += len(data)
            await writer.drain()
    except (ConnectionError, OSError):
        writer.close()
        return
    if writer.can_write_eof():
        with contextlib.suppress(Exception):
            writer.write_eof()
            return
    writer.close()


async def splice(
    guest_reader: asyncio.StreamReader,
    guest_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    count: ByteCount | None = None,
    *,
    idle_timeout: float = IDLE_TIMEOUT,
) -> ByteCount:
    counted = count if count is not None else ByteCount()

    def idle() -> None:
        guest_writer.transport.abort()
        upstream_writer.transport.abort()

    watch = asyncio.create_task(watch_idle(counted, idle_timeout, idle))
    try:
        await asyncio.gather(
            _pump(guest_reader, upstream_writer, counted, "up"),
            _pump(upstream_reader, guest_writer, counted, "down"),
        )
    finally:
        watch.cancel()
        await close_writer(upstream_writer, guest_writer)
    return counted
