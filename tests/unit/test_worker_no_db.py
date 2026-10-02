"""Split worker paths never touch the database (#449)."""

import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from apipi.worker.execution import LocalExecution
from apipi.worker.hub import _seed_reaper_ttl
from apipi.worker.outbox import Outbox
from apipi.worker.pi.artifacts import reap_workspaces


def _execution(settings, **kwargs: Any) -> LocalExecution:
    pool = SimpleNamespace(
        sandbox_seen_ids=lambda: [],
        lifecycle=None,
        on_kill=None,
        on_transition=None,
    )
    return LocalExecution(
        settings,
        pool=pool,  # ty: ignore[invalid-argument-type]
        harness=SimpleNamespace(),
        isolation=SimpleNamespace(),  # ty: ignore[invalid-argument-type]
        hub=SimpleNamespace(),  # ty: ignore[invalid-argument-type]
        store=None,
        outbox=Outbox(),
        **kwargs,
    )


async def test_transition_reports_envelope_without_db(settings) -> None:
    execution = _execution(settings)
    session_id = uuid.uuid4()
    await execution._sandbox_transition(
        session_id,
        "ready",
        {"tenant_id": uuid.uuid4(), "boot_ms": 7, "live": True},
    )
    outbox = execution.outbox
    assert outbox is not None
    pending = outbox.pending(session_id)
    assert len(pending) == 1
    assert pending[0]["type"] == "sandbox.status"
    assert pending[0]["payload"]["status"] == "ready"
    assert pending[0]["payload"]["boot_ms"] == 7


async def test_seen_loop_uses_hook_without_db(settings, monkeypatch) -> None:
    import asyncio
    import datetime

    import apipi.services.sandbox_status as sandbox_status

    monkeypatch.setattr(
        sandbox_status, "SEEN_INTERVAL", datetime.timedelta(seconds=0.01)
    )
    seen: list[list[uuid.UUID]] = []
    called = asyncio.Event()

    async def hook(session_ids: list[uuid.UUID]) -> None:
        seen.append(session_ids)
        called.set()

    execution = _execution(settings)
    execution.seen_hook = hook
    task = asyncio.create_task(execution.sandbox_seen_loop())
    try:
        await asyncio.wait_for(called.wait(), timeout=5)
    finally:
        task.cancel()
    assert seen and seen[0] == []


def test_seed_reaper_ttl_ignores_garbage() -> None:
    execution = SimpleNamespace(_context_ttl={})
    before = time.time()
    _seed_reaper_ttl(
        execution,
        {
            str(uuid.uuid4()): {"idle_ttl_seconds": 60.0, "env_type": "openai_hosted"},
            "nope": {"idle_ttl_seconds": 1.0},
            str(uuid.uuid4()): "nope",
        },
    )
    assert len(execution._context_ttl) == 1
    seconds, at, env_type = next(iter(execution._context_ttl.values()))
    assert seconds == 60.0
    assert at >= before
    assert env_type == "openai_hosted"


async def _workspace(settings, tenant_id: uuid.UUID, session_id: uuid.UUID) -> Path:
    from apipi.worker.pi.dirs import sessions_root

    path = sessions_root(settings) / str(tenant_id) / str(session_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "outputs").mkdir(exist_ok=True)
    (path / "outputs" / "a.txt").write_text("a")
    return path


class _Pool:
    def alive(self, session_id: uuid.UUID) -> bool:
        return False

    def held(self, session_id: uuid.UUID) -> bool:
        return False


async def test_reaper_skips_unknown_without_db(settings) -> None:
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    path = await _workspace(settings, tenant_id, session_id)
    wiped = await reap_workspaces(
        settings,
        None,
        _Pool(),  # ty: ignore[invalid-argument-type]
        ttl_overrides={},
        allow_db=False,
    )
    assert wiped == []
    assert path.is_dir()


async def test_reaper_wipes_with_inventory_ttl(settings) -> None:
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    path = await _workspace(settings, tenant_id, session_id)
    wiped = await reap_workspaces(
        settings,
        None,
        _Pool(),  # ty: ignore[invalid-argument-type]
        ttl_overrides={
            str(session_id): (0.0, time.time() - 30, "openai_hosted"),
        },
        allow_db=False,
    )
    assert wiped == [str(session_id)]
    assert not path.exists()
