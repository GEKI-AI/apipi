"""Split worker paths never touch the database (#449)."""

import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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


def _block_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the worker constructs storage clients."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("split worker must not construct storage clients")

    import apipi.services.runtime as runtime
    import apipi.store.blobs as blobs
    import apipi.store.engine as engine

    monkeypatch.setattr(engine, "create_engine", _boom)
    monkeypatch.setattr(engine, "Store", _boom)
    monkeypatch.setattr(runtime, "object_store", _boom)
    monkeypatch.setattr(blobs, "object_store", _boom)
    monkeypatch.setattr(blobs, "blob_store", _boom)
    monkeypatch.setattr(blobs, "S3Store", _boom)


def test_prepare_worker_refuses_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Any
) -> None:
    from apipi.cli import prepare_worker
    from apipi.config import ConfigError

    monkeypatch.setattr("apipi.cli.probe_model_host", lambda _settings: None)
    monkeypatch.setattr("apipi.cli.probe_run_mode", lambda _settings: None)
    token_file = tmp_path / "worker.token"
    token_file.write_text("secret\n")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://db/apipi")
    with pytest.raises(ConfigError, match="no longer uses DATABASE_URL"):
        prepare_worker(
            settings.model_copy(
                update={"worker_token_file": str(token_file), "run_mode": "none"}
            )
        )


async def test_split_turn_needs_no_store_or_object_credentials(
    store: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apipi.services.event_bus import InMemoryEventBus
    from apipi.services.runtime import FakeHarness
    from apipi.services.sink import OutboxSink
    from apipi.services.turn_context import build_turn_context
    from apipi.store.repo import create_session, create_tenant
    from apipi.worker.execution import LocalExecution
    from apipi.worker.pi.isolation import load_isolation
    from apipi.worker.pi.pool import PiPool

    monkeypatch.delenv("DATABASE_URL", raising=False)
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, model="m1", status="idle", environment={"type": "none"}
        )
    tenant_id, session_id = tenant.id, row.id
    context = await build_turn_context(store, settings, tenant_id, session_id)
    _block_storage(monkeypatch)
    outbox = Outbox()
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tool"}]
    execution = LocalExecution(
        settings,
        pool=PiPool(settings),
        harness=harness,
        isolation=load_isolation("none"),
        hub=InMemoryEventBus(),
        store=None,
        outbox=outbox,
    )
    await execution.run_turn(
        tenant_id,
        session_id,
        "hello",
        turn_context=context,
        sink=OutboxSink(outbox, tenant_id, session_id),
    )
    pending = outbox.pending(session_id)
    assert [item["type"] for item in pending].count("turn.status") >= 1
    assert any(
        item["type"] == "event"
        and item["payload"].get("type") == "agent.session.turn.item.added"
        and (item["payload"].get("data") or {}).get("item_type") == "mcp_call"
        for item in pending
    )


async def test_split_boot_hosted_needs_no_store(
    store: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apipi.services.event_bus import InMemoryEventBus
    from apipi.services.runtime import FakeHarness
    from apipi.services.turn_context import build_turn_context
    from apipi.store.repo import create_session, create_tenant
    from apipi.worker.execution import LocalExecution
    from apipi.worker.pi.dirs import sessions_root
    from apipi.worker.pi.isolation import load_isolation
    from apipi.worker.pi.pool import PiPool

    monkeypatch.delenv("DATABASE_URL", raising=False)
    workspace = sessions_root(settings) / "tenant" / "session"
    workspace.mkdir(parents=True, exist_ok=True)
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db,
            tenant.id,
            model="m1",
            status="idle",
            environment={"type": "openai_hosted", "directory": str(workspace)},
        )
    context = await build_turn_context(store, settings, tenant.id, row.id)
    _block_storage(monkeypatch)
    execution = LocalExecution(
        settings,
        pool=PiPool(settings),
        harness=FakeHarness(),
        isolation=load_isolation("none"),
        hub=InMemoryEventBus(),
        store=None,
        outbox=Outbox(),
    )
    spawned: list[dict[str, Any]] = []

    async def _fake_get(session_id: uuid.UUID, **kwargs: Any) -> None:
        spawned.append({"session_id": session_id, **kwargs})

    monkeypatch.setattr(execution.pool, "get", _fake_get)
    await execution.boot_hosted(tenant.id, row.id, turn_context=context)
    assert spawned and spawned[0]["session_id"] == row.id
    assert spawned[0]["env_type"] == "openai_hosted"


async def test_escaped_turn_reported_without_db(settings: Any) -> None:
    from apipi.gateway.errors import ApiError

    execution = _execution(settings)
    assert execution.outbox is not None
    session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    from apipi.worker.hub import _report_escaped_turn

    await _report_escaped_turn(
        execution, tenant_id, session_id, ApiError("invalid_request", "nope")
    )
    pending = execution.outbox.pending(session_id)
    assert any(
        item["type"] == "event"
        and item["payload"].get("type") == "agent.session.failed"
        for item in pending
    )
