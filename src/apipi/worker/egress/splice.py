import asyncio
import contextlib
from dataclasses import dataclass

from apipi.worker.egress.sockets import CHUNK, close_writer


@dataclass
class ByteCount:
    up: int = 0
    down: int = 0


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
) -> ByteCount:
    counted = count if count is not None else ByteCount()
    try:
        await asyncio.gather(
            _pump(guest_reader, upstream_writer, counted, "up"),
            _pump(upstream_reader, guest_writer, counted, "down"),
        )
    finally:
        await close_writer(upstream_writer)
        await close_writer(guest_writer)
    return counted
