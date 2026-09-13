import json
import uuid
from asyncio.subprocess import Process
from pathlib import Path
from typing import cast

from apipi.config import Settings
from apipi.pi.harness import PiHarness
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc


class _Stdin:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None


class _Process:
    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.stdout = None
        self.returncode = None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )


async def test_abort_sends_rpc() -> None:
    inner = _Process()
    proc = PiProc(cast(Process, inner))
    await proc.abort()
    assert json.loads(inner.stdin.buf.decode().strip()) == {"type": "abort"}


async def test_abort_without_process(tmp_path: Path) -> None:
    pool = PiPool(_settings(tmp_path))
    await PiHarness(pool).abort(uuid.uuid4())
    assert pool.peek(uuid.uuid4()) is None


async def test_abort_when_process_exists(tmp_path: Path) -> None:
    inner = _Process()
    proc = PiProc(cast(Process, inner))
    pool = PiPool(_settings(tmp_path))
    session_id = uuid.uuid4()
    pool._procs[session_id] = proc
    await PiHarness(pool).abort(session_id)
    assert json.loads(inner.stdin.buf.decode().strip()) == {"type": "abort"}
    assert pool.peek(session_id) is proc
