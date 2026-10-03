import asyncio
import json
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from tests.support.prom import metric_line

from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.worker.client import run_worker
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
            return json.dumps(
                {
                    "ok": True,
                    "worker_id": str(uuid.uuid4()),
                    "lease_ttl_seconds": 30,
                    "heartbeat_seconds": 10,
                }
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _Connect:
    async def __aenter__(self) -> _Sock:
        return _Sock()

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Execution:
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

    async def sandbox_seen_loop(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


async def test_run_worker_reaps_idle_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metrics = Metrics()
    (tmp_path / "worker.token").write_text("secret\n")
    settings = Settings(
        run_mode="none",
        worker_token_file=str(tmp_path / "worker.token"),
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
    monkeypatch.setattr(
        "apipi.worker.client.websockets.connect", lambda *_a, **_k: _Connect()
    )
    monkeypatch.setattr(
        "apipi.worker.client._install_drain_signals", lambda _event: None
    )

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


async def test_run_worker_reports_lifecycle_over_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from apipi.worker.lifecycle import OutboxLifecycleReporter

    (tmp_path / "worker.token").write_text("secret\n")
    monkeypatch.setenv("APIPI_LIFECYCLE_EXPORT_URL", "http://export.test/life")
    settings = Settings(
        run_mode="none",
        worker_token_file=str(tmp_path / "worker.token"),
        lifecycle_heartbeat="off",
    )
    pool = PiPool(settings)
    started = asyncio.Event()
    closed = asyncio.Event()

    class _LifeExecution(_Execution):
        async def sandbox_seen_loop(self) -> None:
            started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            closed.set()

    execution = _LifeExecution(pool, asyncio.Event())
    monkeypatch.setattr(
        "apipi.worker.execution.local_execution",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        "apipi.worker.execution.worker_observability",
        lambda _settings: (None, None),
    )
    monkeypatch.setattr(
        "apipi.worker.client.websockets.connect", lambda *_a, **_k: _Connect()
    )
    monkeypatch.setattr(
        "apipi.worker.client._install_drain_signals", lambda _event: None
    )
    task = asyncio.create_task(run_worker(settings, url="http://127.0.0.1:8000"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        deadline = time.monotonic() + 2
        reporter = None
        while time.monotonic() < deadline:
            current = pool.lifecycle
            if (
                type(current) is OutboxLifecycleReporter
                and current.worker_id is not None
            ):
                reporter = current
                break
            await asyncio.sleep(0.02)
        assert reporter is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    assert any(
        "API-only lifecycle settings" in record.message for record in caplog.records
    )


async def test_run_worker_warns_when_the_lease_ttl_is_set_on_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "worker.token").write_text("secret\n")
    settings = Settings(
        run_mode="none",
        worker_token_file=str(tmp_path / "worker.token"),
        worker_lease_ttl=timedelta(seconds=10),
    )
    started = asyncio.Event()

    class _TtlExecution(_Execution):
        async def sandbox_seen_loop(self) -> None:
            started.set()
            await asyncio.Event().wait()

    execution = _TtlExecution(PiPool(settings), asyncio.Event())
    monkeypatch.setattr(
        "apipi.worker.execution.local_execution",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        "apipi.worker.execution.worker_observability",
        lambda _settings: (None, None),
    )
    monkeypatch.setattr(
        "apipi.worker.client.websockets.connect", lambda *_a, **_k: _Connect()
    )
    monkeypatch.setattr(
        "apipi.worker.client._install_drain_signals", lambda _event: None
    )
    with caplog.at_level("WARNING", logger="apipi.worker"):
        task = asyncio.create_task(run_worker(settings, url="http://127.0.0.1:8000"))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert any(
        getattr(record, "event", None) == "worker.lease_ttl.ignored"
        for record in caplog.records
    )
