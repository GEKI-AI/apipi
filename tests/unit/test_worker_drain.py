import asyncio
import uuid
from datetime import timedelta
from pathlib import Path
from typing import cast

from tests.support.fake_proc import FakeProc

from apipi.config import Settings
from apipi.worker.client import drain_idle, drain_timeout_seconds, worker_heartbeat
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc


def test_worker_heartbeat_omits_drain_by_default() -> None:
    payload = worker_heartbeat(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        )
    )
    assert payload["type"] == "heartbeat"
    assert "drain" not in payload
    assert payload["accepts"] == ["none"]


def test_worker_heartbeat_sets_drain_true() -> None:
    payload = worker_heartbeat(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        ),
        drain=True,
    )
    assert payload["drain"] is True
    assert payload["run_mode"] == "none"


def test_drain_timeout_defaults_to_idle_ttl() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
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
    pool._procs[idle] = cast(PiProc, FakeProc())
    pool._procs[busy] = cast(PiProc, FakeProc())
    pool.hold(busy)
    await pool.kill_unheld(reason="idle")
    assert pool.alive(idle) is False
    assert pool.alive(busy) is True


async def test_drain_reason_is_not_idle() -> None:
    from apipi.worker.lifecycle import OutboxLifecycleReporter
    from apipi.worker.outbox import Outbox

    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    pool = PiPool(settings)
    outbox = Outbox()
    pool.lifecycle = OutboxLifecycleReporter(outbox)
    sid = uuid.uuid4()
    pool._procs[sid] = cast(PiProc, FakeProc())
    pool._live[sid] = {
        "session_id": sid,
        "start_seq": 1,
        "started_at": "2026-09-27T18:00:00.000Z",
        "born": 0.0,
        "sandbox_size": "S",
    }
    pool._born[sid] = 0.0
    await pool.kill_unheld(reason="drain")
    envelope = outbox.pending(sid)[-1]
    assert envelope["type"] == "lifecycle.stop"
    assert envelope["payload"]["reason"] == "drain"
    text = Path("src/apipi/worker/client.py").read_text(encoding="utf-8")
    assert 'kill_unheld(reason="drain")' in text
    assert 'kill_unheld(reason="idle")' not in text
