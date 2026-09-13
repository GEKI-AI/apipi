import json
from asyncio.streams import StreamReader
from asyncio.subprocess import Process
from typing import cast

from apipi.pi.proc import PiProc


class _Stdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _Process:
    def __init__(self) -> None:
        self.stdin = None
        self.stdout = None
        self.returncode = None


async def test_events_read_jsonl_line_over_64kib() -> None:
    payload = {"type": "message_update", "pad": "x" * 70000}
    line = json.dumps(payload).encode() + b"\n"
    assert len(line) > 65536
    inner = _Process()
    proc = PiProc(
        cast(Process, inner),
        stdout=cast(StreamReader, _Stdout([line[:40000], line[40000:]])),
    )
    events = [event async for event in proc._events()]
    assert events == [payload]


async def test_events_split_on_lf_only() -> None:
    first = {"type": "agent_start"}
    second = {"type": "agent_settled"}
    blob = json.dumps(first).encode() + b"\n" + json.dumps(second).encode() + b"\n"
    inner = _Process()
    proc = PiProc(cast(Process, inner), stdout=cast(StreamReader, _Stdout([blob])))
    events = [event async for event in proc._events()]
    assert events == [first, second]
