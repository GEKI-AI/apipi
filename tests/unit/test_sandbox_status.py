import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest

from apipi.config import CapacityError, Settings
from apipi.services.runtime import EventHub
from apipi.services.sandbox_status import (
    STALE_AFTER,
    eager_boot_enabled,
    record_transition,
)
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import create_session, create_tenant, get_session
from apipi.worker.pi.pool import PiPool


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )


class _Proc:
    def __init__(self) -> None:
        self.alive = True
        self.image = None
        self.vm_id = None

    async def terminate(self) -> None:
        self.alive = False


async def _hosted(store: Store) -> tuple[uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name=f"sandbox-{uuid.uuid4()}")
        row = await create_session(
            db,
            tenant.id,
            environment={
                "type": "openai_hosted",
                "id": str(uuid.uuid4()),
                "sandbox_size": "S",
                "sandbox_image": "default",
            },
        )
        return tenant.id, row.id


async def test_pool_transitions_cold_warm_and_stop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fake_spawn(*_args: object, **_kwargs: Any) -> _Proc:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    hub = EventHub()
    pool = PiPool(_settings())

    async def on_transition(
        session_id: uuid.UUID, phase: str, fields: dict[str, Any]
    ) -> None:
        await record_transition(store, hub, session_id, phase, fields)

    pool.on_transition = on_transition
    tenant_id, session_id = await _hosted(store)
    await pool.get(
        session_id,
        cwd=None,
        tools=True,
        tenant_id=tenant_id,
        env_type="openai_hosted",
        image="default",
    )
    await pool.get(
        session_id,
        cwd=None,
        tools=True,
        tenant_id=tenant_id,
        env_type="openai_hosted",
        image="default",
    )
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
        row = await get_session(db, tenant_id, session_id)
    types = [event.type for event in events]
    assert types == [
        "agent.session.environment.pending",
        "agent.session.environment.connected",
    ]
    assert events[0].data["sandbox"]["cold"] is True
    assert events[0].data["sandbox"]["cause"] == "spawn"
    connected = events[1].data["sandbox"]
    assert connected["boot_ms"] >= 0
    assert "lock_wait_ms" in connected
    assert "setup_ms" in connected
    assert row is not None
    assert row.sandbox_state == "ready"
    assert row.required_actions == []
    assert calls == 1

    await pool.kill(session_id, reason="idle")
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
        row = await get_session(db, tenant_id, session_id)
    assert events[-1].type == "agent.session.environment.disconnected"
    assert events[-1].data["sandbox"]["reason"] == "idle"
    assert row is not None
    assert row.sandbox_state == "stopped"
    assert row.required_actions == []


async def test_pool_reports_every_stop_reason(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_spawn(*_args: object, **_kwargs: Any) -> _Proc:
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    hub = EventHub()
    pool = PiPool(_settings())

    async def on_transition(
        session_id: uuid.UUID, phase: str, fields: dict[str, Any]
    ) -> None:
        await record_transition(store, hub, session_id, phase, fields)

    pool.on_transition = on_transition
    reasons = {
        "session": "stop",
        "respawn": "respawn",
        "memory": "memory",
        "crash": "crash",
        "drain": "drain",
        "shutdown": "shutdown",
    }
    for reason, public in reasons.items():
        tenant_id, session_id = await _hosted(store)
        await pool.get(
            session_id,
            cwd=None,
            tools=True,
            tenant_id=tenant_id,
            env_type="openai_hosted",
        )
        await pool.kill(session_id, reason=reason)
        async with store.session() as db:
            events = await list_events(db, tenant_id, session_id)
        assert events[-1].data["sandbox"]["reason"] == public


async def test_none_env_emits_no_sandbox_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def fake_spawn(*_args: object, **_kwargs: Any) -> _Proc:
        return _Proc()

    async def on_transition(
        _session_id: uuid.UUID, phase: str, _fields: dict[str, Any]
    ) -> None:
        seen.append(phase)

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    pool = PiPool(_settings())
    pool.on_transition = on_transition
    await pool.get(uuid.uuid4(), cwd=None, tools=False, env_type="none")
    await pool.get(uuid.uuid4(), cwd=None, tools=False, env_type="self_hosted")
    assert seen == []


async def test_pool_kill_notifies_lease_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apipi.env.hub import EnvironmentHub
    from apipi.services.runtime import EventHub
    from apipi.worker.execution import LocalExecution
    from apipi.worker.pi.isolation import load_isolation

    async def fake_spawn(*_args: object, **_kwargs: Any) -> _Proc:
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    settings = _settings()
    pool = PiPool(settings)
    seen: list[uuid.UUID] = []

    async def note(session_id: uuid.UUID) -> None:
        seen.append(session_id)

    execution = LocalExecution(
        settings,
        pool=pool,
        harness=object(),
        isolation=load_isolation("none"),
        hub=EventHub(),
        env_hub=EnvironmentHub(),
    )
    execution.note_stopped = note
    session_id = uuid.uuid4()
    await pool.get(session_id, cwd=None, tools=False)
    await pool.kill(session_id, reason="idle")
    assert seen == [session_id]


async def test_warm_attach_is_not_blocked_by_another_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_spawn(*_args: object, **_kwargs: Any) -> _Proc:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    pool = PiPool(_settings())
    cold = uuid.uuid4()
    warm = uuid.uuid4()
    first = asyncio.create_task(pool.get(warm, cwd=None, tools=True))
    await started.wait()
    release.set()
    await first
    release.clear()
    started.clear()
    delayed = asyncio.create_task(pool.get(cold, cwd=None, tools=True))
    await started.wait()
    attached = asyncio.create_task(pool.get(warm, cwd=None, tools=True))
    await asyncio.wait_for(attached, timeout=1)
    assert calls == 2
    release.set()
    await delayed


async def test_stale_seen_at_is_worker_lost(store: Store) -> None:
    from apipi.services.sandbox_status import expire_if_stale
    from apipi.services.sessions import session_body

    tenant_id, session_id = await _hosted(store)
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        row.sandbox_state = "ready"
        row.sandbox_seen_at = utc_now() - STALE_AFTER - timedelta(seconds=1)
        await db.flush()
        hub = EventHub()
        await expire_if_stale(db, hub, tenant_id, row)
        body = session_body(row)
    assert body["environment"]["status"] == "disconnected"
    assert body["environment"]["sandbox"]["state"] == "stopped"
    assert body["environment"]["sandbox"]["reason"] == "worker_lost"


def test_eager_boot_overrides() -> None:
    settings = _settings()
    assert (
        eager_boot_enabled(
            settings,
            session_metadata=None,
            agent_metadata=None,
            session_defaults=None,
        )
        is False
    )
    settings.sandbox_eager_boot = True
    assert (
        eager_boot_enabled(
            settings,
            session_metadata={"apipi.sandbox_eager_boot": False},
            agent_metadata={"apipi.sandbox_eager_boot": True},
            session_defaults=None,
        )
        is False
    )
    assert (
        eager_boot_enabled(
            settings,
            session_metadata={},
            agent_metadata={"apipi.sandbox_eager_boot": "on"},
            session_defaults={"metadata": {"apipi.sandbox_eager_boot": False}},
        )
        is True
    )


async def test_capacity_failure_marks_environment_failed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apipi.env.hub import EnvironmentHub
    from apipi.worker.execution import LocalExecution
    from apipi.worker.pi.isolation import load_isolation

    async def fake_kwargs(*_args: object, **_kwargs: Any) -> dict[str, Any]:
        return {
            "cwd": None,
            "tools": True,
            "env_type": "openai_hosted",
            "tenant_id": tenant_id,
        }

    async def fake_spawn(*_args: object, **_kwargs: Any) -> _Proc:
        raise CapacityError("Too many live sessions", code="capacity")

    monkeypatch.setattr("apipi.services.runtime.load_boot_kwargs", fake_kwargs)
    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    settings = _settings()
    pool = PiPool(settings)
    hub = EventHub()
    execution = LocalExecution(
        settings,
        pool=pool,
        harness=object(),
        isolation=load_isolation("none"),
        hub=hub,
        env_hub=EnvironmentHub(),
        store=store,
    )
    tenant_id, session_id = await _hosted(store)
    await execution.boot_hosted(tenant_id, session_id)
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
        row = await get_session(db, tenant_id, session_id)
    assert any(
        event.type == "agent.session.environment.failed"
        and event.data["sandbox"]["state"] == "failed"
        for event in events
    )
    assert row is not None
    assert row.sandbox_state == "failed"
    assert row.status == "failed"
