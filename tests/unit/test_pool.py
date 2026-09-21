import time
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway.metrics import Metrics
from apipi.gateway.otel import Tracing
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc


class _Alive:
    alive = True


def test_has_capacity_counts_live_procs() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            max_sessions=1,
        )
    )
    first = uuid.uuid4()
    second = uuid.uuid4()
    assert pool.has_capacity(first)
    pool._procs[first] = cast(PiProc, _Alive())
    assert pool.has_capacity(first)
    assert not pool.has_capacity(second)
    assert pool.live() == 1
    assert pool.capacity_code(second) == "capacity"


def test_has_capacity_ram_cap() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            max_sessions=8,
            worker_memory_mb=512,
            microvm_mem_mib=512,
        )
    )
    first = uuid.uuid4()
    second = uuid.uuid4()
    assert pool.has_capacity(first)
    pool._procs[first] = cast(PiProc, _Alive())
    assert pool.has_capacity(first)
    assert not pool.has_capacity(second)
    assert pool.capacity_code(second) == "capacity"


def test_has_capacity_mixed_session_mem() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            max_sessions=8,
            worker_memory_mb=2560,
            microvm_mem_mib=512,
        )
    )
    first = uuid.uuid4()
    second = uuid.uuid4()
    pool._procs[first] = cast(PiProc, _Alive())
    pool._mem[first] = 2048
    assert pool.capacity_code(first, session_mem_mib=2048) is None
    assert pool.capacity_code(second, session_mem_mib=512) is None
    assert pool.capacity_code(second, session_mem_mib=1024) == "capacity"


def test_has_capacity_per_tenant() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            max_sessions=8,
            max_sessions_per_tenant=1,
        )
    )
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    first = uuid.uuid4()
    second = uuid.uuid4()
    other = uuid.uuid4()
    pool._procs[first] = cast(PiProc, _Alive())
    pool._tenants[first] = tenant_a
    assert pool.has_capacity(first, tenant_a)
    assert not pool.has_capacity(second, tenant_a)
    assert pool.capacity_code(second, tenant_a) == "capacity_tenant"
    assert pool.has_capacity(other, tenant_b)
    assert pool.live_for(tenant_a) == 1
    assert pool.live_for(tenant_b) == 0


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )


class _Proc:
    def __init__(self) -> None:
        self.alive = True

    async def terminate(self) -> None:
        self.alive = False


async def test_pool_respawns_when_instructions_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[str | None] = []

    async def fake_spawn(*_args: object, **kwargs: Any) -> _Proc:
        spawned.append(kwargs.get("instructions"))
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    pool = PiPool(_settings())
    session_id = uuid.uuid4()
    await pool.get(session_id, cwd=None, tools=True, instructions="a")
    await pool.get(session_id, cwd=None, tools=True, instructions="a")
    await pool.get(session_id, cwd=None, tools=True, instructions="b")
    assert spawned == ["a", "b"]


async def test_pool_treats_empty_instructions_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[str | None] = []

    async def fake_spawn(*_args: object, **kwargs: Any) -> _Proc:
        spawned.append(kwargs.get("instructions"))
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    pool = PiPool(_settings())
    session_id = uuid.uuid4()
    await pool.get(session_id, cwd=None, tools=True, instructions=None)
    await pool.get(session_id, cwd=None, tools=True, instructions="")
    assert spawned == [None]


async def test_hosted_reap_uses_sandbox_ttl() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            idle_ttl=timedelta(seconds=1),
            workspace_ttl=timedelta(hours=1),
        )
    )
    hosted = uuid.uuid4()
    other = uuid.uuid4()
    pool._procs[hosted] = cast(PiProc, _Proc())
    pool._procs[other] = cast(PiProc, _Proc())
    pool._env_types[hosted] = "openai_hosted"
    pool._env_types[other] = "none"
    pool._last[hosted] = time.monotonic() - 30
    pool._last[other] = time.monotonic() - 30
    await pool.reap()
    assert hosted in pool._procs
    assert other not in pool._procs


def test_hold_and_release() -> None:
    pool = PiPool(_settings())
    sid = uuid.uuid4()
    assert not pool.held(sid)
    pool.hold(sid)
    assert pool.held(sid)
    assert not pool.alive(sid)
    pool.release(sid)
    assert not pool.held(sid)
    pool.release(sid)


async def test_get_emits_sandbox_attach_span() -> None:
    exporter = InMemorySpanExporter()
    tracing = Tracing(exporter=exporter)
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        ),
        tracing=tracing,
    )
    sid = uuid.uuid4()
    pool._procs[sid] = cast(PiProc, _Alive())
    pool._spawn_tools[sid] = True
    try:
        await pool.get(sid, cwd=None, tools=True)
        names = [span.name for span in exporter.get_finished_spans()]
        assert names == ["sandbox.attach"]
        assert dict(exporter.get_finished_spans()[0].attributes or {})[
            "session_id"
        ] == str(sid)
    finally:
        tracing.shutdown()


async def test_get_emits_sandbox_boot_span(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    tracing = Tracing(exporter=exporter)
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        ),
        tracing=tracing,
    )

    async def _spawn(*_args: object, **_kwargs: object) -> PiProc:
        return cast(PiProc, _Alive())

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", _spawn)
    sid = uuid.uuid4()
    try:
        await pool.get(sid, cwd=None, tools=True)
        names = [span.name for span in exporter.get_finished_spans()]
        assert names == ["sandbox.boot"]
    finally:
        tracing.shutdown()


async def test_hosted_reap_kills_after_sandbox_ttl() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            idle_ttl=timedelta(hours=24),
            workspace_ttl=timedelta(seconds=1),
        )
    )
    hosted = uuid.uuid4()
    pool._procs[hosted] = cast(PiProc, _Proc())
    pool._env_types[hosted] = "openai_hosted"
    pool._last[hosted] = time.monotonic() - 30
    await pool.reap()
    assert hosted not in pool._procs


class _HostProc(_Proc):
    vm_id = None

    def __init__(self, pid: int) -> None:
        super().__init__()
        self.process = type("P", (), {"pid": pid})()


async def test_enforce_memory_kills_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = Metrics()
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            pi_mem_mib=1,
        ),
        metrics=metrics,
    )
    over = uuid.uuid4()
    under = uuid.uuid4()
    guest = uuid.uuid4()
    pool._procs[over] = cast(PiProc, _HostProc(11))
    pool._procs[under] = cast(PiProc, _HostProc(12))
    guest_proc = _HostProc(13)
    guest_proc.vm_id = "vm-1"
    pool._procs[guest] = cast(PiProc, guest_proc)

    def fake_rss(pid: int, **_kwargs: object) -> tuple[int, int]:
        if pid == 11:
            return (2 * 1024 * 1024, 0)
        return (100, 0)

    monkeypatch.setattr("apipi.worker.procmem.read_group_rss_pss", fake_rss)
    await pool.enforce_memory()
    assert over not in pool._procs
    assert under in pool._procs
    assert guest in pool._procs
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_pi_kill_total", reason="memory").endswith(" 1.0")


async def test_enforce_memory_skips_when_unset() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        )
    )
    sid = uuid.uuid4()
    pool._procs[sid] = cast(PiProc, _HostProc(11))
    await pool.enforce_memory()
    assert sid in pool._procs
