import asyncio
import uuid
from datetime import timedelta
from typing import cast

from apipi.config import Settings
from apipi.worker.hub import drain_idle, drain_timeout_seconds, worker_heartbeat
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc


class _Proc:
    alive = True
    vm_id = None

    async def terminate(self) -> None:
        self.alive = False


def test_worker_heartbeat_omits_drain_by_default() -> None:
    payload = worker_heartbeat(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="chat",
        )
    )
    assert payload["type"] == "heartbeat"
    assert "drain" not in payload


def test_worker_heartbeat_sets_drain_true() -> None:
    payload = worker_heartbeat(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="chat",
        ),
        drain=True,
    )
    assert payload["drain"] is True
    assert payload["run_mode"] == "chat"


def test_drain_timeout_defaults_to_idle_ttl() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="chat",
        idle_ttl=timedelta(minutes=15),
    )
    assert drain_timeout_seconds(settings, None) == 900.0
    assert drain_timeout_seconds(settings, 30.0) == 30.0


async def test_drain_idle_requires_no_live_and_no_commands() -> None:
    assert drain_idle(0, set()) is True
    assert drain_idle(1, set()) is False
    task = asyncio.create_task(asyncio.sleep(0))
    try:
        assert drain_idle(0, {task}) is False
    finally:
        await task


async def test_kill_unheld_skips_held_sessions() -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        )
    )
    idle = uuid.uuid4()
    busy = uuid.uuid4()
    pool._procs[idle] = cast(PiProc, _Proc())
    pool._procs[busy] = cast(PiProc, _Proc())
    pool.hold(busy)
    await pool.kill_unheld(reason="idle")
    assert pool.alive(idle) is False
    assert pool.alive(busy) is True
