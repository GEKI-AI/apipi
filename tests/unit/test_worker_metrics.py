import json
import uuid
from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway.metrics import Metrics
from apipi.worker.cgroup import jailer_cgroup_dir, read_cgroup
from apipi.worker.pi.guest import guest_sample, guest_sample_bytes
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc
from apipi.worker.scrape import metrics_app


class _Alive:
    alive = True
    vm_id = None
    pull_metrics = None

    async def terminate(self) -> None:
        return None


def test_worker_util_and_sandbox_series() -> None:
    metrics = Metrics()
    metrics.set_worker_util(
        capacity=32, sessions=2, memory_mib_used=1024, memory_mib_total=16384
    )
    metrics.observe_sandbox_boot(size="S", result="ok", seconds=0.2)
    metrics.observe_sandbox_destroy(size="S", hold_seconds=12)
    metrics.set_sandboxes_active({"S": 2, "M": 0, "L": 0})
    metrics.set_guest_cgroup(
        size="S",
        memory_bytes=100,
        memory_limit_bytes=512 * 1024 * 1024,
        cpu_seconds=1.5,
    )
    metrics.set_guest_sample(
        size="S",
        mem_available_bytes=50,
        load=0.2,
        workspace_used_bytes=10,
        workspace_avail_bytes=90,
    )
    body = metrics.scrape().decode()
    assert "apipi_worker_capacity 32.0" in body
    assert "apipi_worker_sessions 2.0" in body
    assert metric_line(
        body, "apipi_sandbox_boot_total", size="S", result="ok"
    ).endswith(" 1.0")
    assert metric_line(body, "apipi_sandbox_destroy_total", size="S").endswith(" 1.0")
    assert metric_line(body, "apipi_sandboxes_active", size="S").endswith(" 2.0")
    assert metric_line(body, "apipi_guest_memory_bytes", size="S").endswith(" 100.0")
    assert "session_id" not in body


async def test_worker_metrics_app_has_no_bearer() -> None:
    metrics = Metrics()
    metrics.set_worker_util(
        capacity=8, sessions=0, memory_mib_used=0, memory_mib_total=4096
    )
    async with AsyncClient(
        transport=ASGITransport(app=metrics_app(metrics)), base_url="http://test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "apipi_worker_capacity" in response.text


def test_cgroup_reader_uses_memory_current(tmp_path: Path) -> None:
    vm_id = "vm-1"
    directory = tmp_path / "jailer" / "firecracker" / vm_id
    directory.mkdir(parents=True)
    (directory / "memory.current").write_text("4096\n")
    (directory / "memory.max").write_text("8192\n")
    (directory / "cpu.stat").write_text("usage_usec 2500000\n")
    assert jailer_cgroup_dir(vm_id, root=tmp_path) == directory
    data = read_cgroup(vm_id, root=tmp_path)
    assert data == {
        "memory_bytes": 4096.0,
        "memory_limit_bytes": 8192.0,
        "cpu_seconds": 2.5,
    }


def test_guest_sample_json(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sample = guest_sample(workspace)
    assert set(sample) == {
        "mem_available_bytes",
        "load_1",
        "workspace_used_bytes",
        "workspace_avail_bytes",
    }
    payload = json.loads(guest_sample_bytes(workspace))
    assert payload["workspace_avail_bytes"] >= 0


async def test_pool_boot_records_sandbox_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = Metrics()
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        ),
        metrics=metrics,
    )

    async def _spawn(*_args: object, **_kwargs: object) -> PiProc:
        return cast(PiProc, _Alive())

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", _spawn)
    sid = uuid.uuid4()
    await pool.get(sid, cwd=None, tools=True)
    pool.refresh_metrics()
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_sandbox_boot_total", size="S", result="ok"
    ).endswith(" 1.0")
    assert "apipi_worker_sessions 1.0" in body
    await pool.kill(sid)
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_sandbox_destroy_total", size="S").endswith(" 1.0")
