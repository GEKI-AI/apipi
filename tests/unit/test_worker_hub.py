import uuid
from unittest.mock import MagicMock

from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway.metrics import Metrics
from apipi.worker.hub import WorkerConnection, WorkerHub


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        microvm_mem_mib=512,
    )


def _conn(*, capacity: int, memory_mb: int, run_mode: str = "chat") -> WorkerConnection:
    return WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=capacity,
        memory_mb=memory_mb,
        run_mode=run_mode,
    )


def test_pick_prefers_more_free_ram() -> None:
    hub = WorkerHub(_settings())
    low = _conn(capacity=8, memory_mb=1024)
    high = _conn(capacity=8, memory_mb=4096)
    hub._conns[low.worker_id] = low
    hub._conns[high.worker_id] = high
    assert hub.pick(run_mode="chat") is high


def test_pick_rejects_ram_cap_with_session_slots() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=8, memory_mb=512)
    conn.leases.add(uuid.uuid4())
    hub._conns[conn.worker_id] = conn
    assert hub.pick(run_mode="chat") is None


def test_pick_rejects_session_cap_with_ram() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=1, memory_mb=8192)
    conn.leases.add(uuid.uuid4())
    hub._conns[conn.worker_id] = conn
    assert hub.pick(run_mode="chat") is None


def test_pick_uses_lease_mem_for_mixed_sizes() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=8, memory_mb=2560)
    lease = uuid.uuid4()
    conn.leases.add(lease)
    conn.lease_mem[lease] = 2048
    hub._conns[conn.worker_id] = conn
    assert hub.pick(512, run_mode="chat") is conn
    assert hub.pick(1024, run_mode="chat") is None


def test_pick_tie_break_fewer_leases() -> None:
    hub = WorkerHub(_settings())
    busy = _conn(capacity=8, memory_mb=4096)
    busy.leases.add(uuid.uuid4())
    idle = _conn(capacity=8, memory_mb=3584)
    hub._conns[busy.worker_id] = busy
    hub._conns[idle.worker_id] = idle
    assert hub.pick(run_mode="chat") is idle


def test_observe_labels_workers_by_run_mode() -> None:
    metrics = Metrics()
    hub = WorkerHub(_settings(), metrics=metrics)
    hub._conns[uuid.uuid4()] = _conn(capacity=1, memory_mb=512, run_mode="chat")
    hub._conns[uuid.uuid4()] = _conn(capacity=1, memory_mb=512, run_mode="microvm")
    hub._observe()
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_workers", run_mode="chat").endswith(" 1.0")
    assert metric_line(body, "apipi_workers", run_mode="microvm").endswith(" 1.0")
    assert metric_line(body, "apipi_worker_leases", run_mode="chat").endswith(" 0.0")
