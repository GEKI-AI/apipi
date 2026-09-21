import asyncio
import contextlib
import os
import signal
import sys
from asyncio.subprocess import Process
from typing import cast

import pytest

from apipi.worker.pi.proc import PiProc


class _Process:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdin = None
        self.stdout = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


def _pid_running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            rest = fh.read().split(")")[-1].split()
        return rest[0] not in {"Z", "X"}
    except FileNotFoundError:
        return False


async def test_terminate_without_process_group_signals_pid() -> None:
    inner = _Process()
    proc = PiProc(cast(Process, inner))
    await proc.terminate()
    assert inner.terminated is True
    assert inner.killed is False
    assert not proc.alive


async def test_terminate_process_group_uses_killpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []
    monkeypatch.setattr("apipi.worker.pi.proc.os.getpgid", lambda pid: pid)

    def fake_killpg(_pid: int, sig: int) -> None:
        signals.append(sig)

    monkeypatch.setattr("apipi.worker.pi.proc.os.killpg", fake_killpg)
    inner = _Process()
    proc = PiProc(cast(Process, inner), process_group=True)
    await proc.terminate()
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert inner.terminated is True
    assert inner.killed is False
    assert not proc.alive


async def test_terminate_process_group_falls_back_when_not_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[int, int]] = []
    monkeypatch.setattr("apipi.worker.pi.proc.os.getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(
        "apipi.worker.pi.proc.os.killpg",
        lambda pid, sig: called.append((pid, sig)),
    )
    inner = _Process()
    proc = PiProc(cast(Process, inner), process_group=True)
    await proc.terminate()
    assert called == []
    assert inner.terminated is True
    assert not proc.alive


_GRANDCHILD = """
import os
import signal
import time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(60)
print(child, flush=True)
while True:
    time.sleep(60)
"""


async def test_terminate_process_group_kills_grandchild() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _GRANDCHILD,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    grandchild = 0
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
        grandchild = int(line.strip())
        assert process.pid is not None
        assert _pid_running(process.pid)
        assert _pid_running(grandchild)
        proc = PiProc(process, process_group=True)
        await proc.terminate()
        assert not proc.alive
        deadline = asyncio.get_running_loop().time() + 2
        while _pid_running(grandchild):
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.05)
        assert not _pid_running(grandchild)
    finally:
        if process.pid is not None and _pid_running(process.pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if grandchild and _pid_running(grandchild):
            with contextlib.suppress(ProcessLookupError):
                os.kill(grandchild, signal.SIGKILL)
