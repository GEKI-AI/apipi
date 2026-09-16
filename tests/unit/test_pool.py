import time
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest

from apipi.config import Settings
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc


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

    monkeypatch.setattr("apipi.pi.pool.spawn_pi", fake_spawn)
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

    monkeypatch.setattr("apipi.pi.pool.spawn_pi", fake_spawn)
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
