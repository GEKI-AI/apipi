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
