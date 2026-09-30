import json
from asyncio.streams import StreamReader
from asyncio.subprocess import Process
from typing import Any, cast

from apipi.worker.pi.proc import PiProc


class _Stdin:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.sent.append(json.loads(data.decode()))

    async def drain(self) -> None:
        return None


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


def _proc(lines: list[dict[str, Any]]) -> tuple[PiProc, _Stdin]:
    blob = b"".join(json.dumps(item).encode() + b"\n" for item in lines)
    inner = _Process()
    stdin = _Stdin()
    proc = PiProc(
        cast(Process, inner),
        stdin=cast(Any, stdin),
        stdout=cast(StreamReader, _Stdout([blob])),
    )
    return proc, stdin


async def test_prompt_started_waits_for_settled() -> None:
    proc, stdin = _proc(
        [
            {
                "type": "response",
                "command": "prompt",
                "success": True,
                "data": {"disposition": "started"},
            },
            {"type": "agent_start"},
            {"type": "agent_settled"},
        ]
    )
    events = [event async for event in proc.prompt("hi")]
    assert stdin.sent[0]["type"] == "prompt"
    assert stdin.sent[0]["id"]
    assert events == [{"type": "agent_start"}, {"type": "agent_settled"}]


async def test_prompt_handled_ends_turn_with_error() -> None:
    proc, _stdin = _proc(
        [
            {
                "type": "response",
                "command": "prompt",
                "success": True,
                "data": {"disposition": "handled"},
            },
        ]
    )
    events = [event async for event in proc.prompt("/mcp")]
    assert events[0]["type"] == "agent_end"
    assert "input_handled_by_command" in events[0]["messages"][0]["errorMessage"]
    assert events[1] == {"type": "agent_settled"}


async def test_prompt_rejected_ends_turn_with_error() -> None:
    proc, _stdin = _proc(
        [
            {
                "type": "response",
                "command": "prompt",
                "success": False,
                "error": "bad prompt",
            },
        ]
    )
    events = [event async for event in proc.prompt("hi")]
    assert events[0]["type"] == "agent_end"
    assert "bad prompt" in events[0]["messages"][0]["errorMessage"]
    assert events[1] == {"type": "agent_settled"}


async def test_prompt_queued_ends_turn_with_error() -> None:
    proc, _stdin = _proc(
        [
            {
                "type": "response",
                "command": "prompt",
                "success": True,
                "data": {"disposition": "queued"},
            },
        ]
    )
    events = [event async for event in proc.prompt("hi")]
    assert events[0]["type"] == "agent_end"
    assert "pi_queued_unexpected" in events[0]["messages"][0]["errorMessage"]
    assert events[1] == {"type": "agent_settled"}
