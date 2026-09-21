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
    metrics.set_host_pi(processes=3, rss_bytes=4096, pss_bytes=2048)
    metrics.observe_pi_spawn("ok")
    metrics.observe_pi_kill("idle")
    body = metrics.scrape().decode()
    assert "apipi_pi_processes 3.0" in body
    assert "apipi_pi_rss_bytes 4096.0" in body
    assert "apipi_pi_pss_bytes 2048.0" in body
    assert metric_line(body, "apipi_pi_spawn_total", result="ok").endswith(" 1.0")
    assert metric_line(body, "apipi_pi_kill_total", reason="idle").endswith(" 1.0")
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
    assert metric_line(body, "apipi_pi_spawn_total", result="ok").endswith(" 1.0")
    await pool.kill(sid)
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_sandbox_destroy_total", size="S").endswith(" 1.0")
    assert metric_line(body, "apipi_pi_kill_total", reason="session").endswith(" 1.0")


def test_procmem_reads_smaps_rollup(tmp_path: Path) -> None:
    proc = tmp_path / "42"
    proc.mkdir()
    (proc / "smaps_rollup").write_text(
        "Rss:              8 kB\nPss:              4 kB\n"
    )
    (proc / "stat").write_text("42 (pi) S 1 42 42 0 0 0\n")
    from apipi.worker.procmem import read_group_rss_pss, read_rss_pss

    assert read_rss_pss(42, proc_root=tmp_path) == (8192, 4096)
    assert read_group_rss_pss(42, proc_root=tmp_path) == (8192, 4096)


async def test_pool_guest_spawn_skips_host_pi_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Guest(_Alive):
        vm_id = "vm-1"

    metrics = Metrics()
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="microvm",
        ),
        metrics=metrics,
    )

    async def _spawn(*_args: object, **_kwargs: object) -> PiProc:
        return cast(PiProc, _Guest())

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", _spawn)
    sid = uuid.uuid4()
    await pool.get(sid, cwd=None, tools=True)
    body = metrics.scrape().decode()
    assert "apipi_pi_spawn_total" not in body or "apipi_pi_spawn_total{" not in body
    await pool.kill(sid)
    body = metrics.scrape().decode()
    assert "apipi_pi_kill_total{" not in body


def test_procmem_falls_back_to_statm(tmp_path: Path) -> None:
    proc = tmp_path / "7"
    proc.mkdir()
    (proc / "statm").write_text("100 3 1 1 0 0 0\n")
    from apipi.worker.procmem import read_rss_pss

    rss, pss = read_rss_pss(7, proc_root=tmp_path)
    assert rss > 0
    assert pss == 0
