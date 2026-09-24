import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import cast

import pytest
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway.metrics import Metrics
from apipi.worker.hub import run_worker
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc


class _Proc:
    alive = True
    vm_id = None

    async def terminate(self) -> None:
        self.alive = False


class _Sock:
    def __init__(self) -> None:
        self._hello = False

    async def send(self, _data: str) -> None:
        return None

    async def recv(self) -> str:
        if not self._hello:
            self._hello = True
            return json.dumps({"ok": True, "worker_id": str(uuid.uuid4())})
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _Connect:
    async def __aenter__(self) -> _Sock:
        return _Sock()

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Store:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def dispose(self) -> None:
        return None


class _Execution:
    stdio_on_host = False
    tracing = None

    def __init__(self, pool: PiPool, workspace: asyncio.Event) -> None:
        self.pool = pool
        self._workspace = workspace

    async def reap_loop(self) -> None:
        await self.pool.reap_loop()

    async def reap_workspace_loop(self) -> None:
        self._workspace.set()
        await asyncio.Event().wait()

    async def observe_loop(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


async def test_run_worker_reaps_idle_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = Metrics()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        worker_token="secret",
        idle_ttl=timedelta(milliseconds=40),
    )
    pool = PiPool(settings, metrics=metrics)
    idle = uuid.uuid4()
    hosted = uuid.uuid4()
    pool._procs[idle] = cast(PiProc, _Proc())
    pool._procs[hosted] = cast(PiProc, _Proc())
    pool._env_types[idle] = "none"
    pool._env_types[hosted] = "openai_hosted"
    pool._last[idle] = time.monotonic() - 10
    pool._last[hosted] = time.monotonic() - 10
    workspace = asyncio.Event()
    execution = _Execution(pool, workspace)

    monkeypatch.setattr(
        "apipi.worker.execution.local_execution",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        "apipi.worker.execution.worker_observability",
        lambda _settings: (None, None),
    )
    monkeypatch.setattr("apipi.store.engine.Store", _Store)
    monkeypatch.setattr(
        "apipi.store.engine.create_engine", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        "apipi.worker.hub.websockets.connect", lambda *_a, **_k: _Connect()
    )
    monkeypatch.setattr("apipi.worker.hub._install_drain_signals", lambda _event: None)

    task = asyncio.create_task(run_worker(settings, url="http://127.0.0.1:8000"))
    try:
        await asyncio.wait_for(workspace.wait(), timeout=2)
        deadline = time.monotonic() + 2
        while pool.alive(idle) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert not pool.alive(idle)
        assert pool.alive(hosted)
        body = metrics.scrape().decode()
        assert metric_line(body, "apipi_pi_kill_total", reason="idle").endswith(" 1.0")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
