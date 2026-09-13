import uuid
from typing import cast

from apipi.config import Settings
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc


class _Alive:
    alive = True


def test_has_capacity_counts_live_procs() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="host",
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
            run_mode="host",
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
