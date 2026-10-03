"""Inventory reconcile and API-owned lease takeover (#449)."""

import uuid
from datetime import UTC, timedelta
from typing import Any
from unittest.mock import MagicMock

from apipi.services.event_bus import create_event_bus
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    get_session,
    set_session_lease,
)
from apipi.worker.commands import CommandDedupe
from apipi.workerhub.hub import WorkerHub


async def _leased(
    store: Store, worker_id: uuid.UUID, **kwargs: Any
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}, **kwargs
        )
        lease_id = uuid.uuid4()
        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + timedelta(seconds=30),
        )
        return tenant.id, row.id, lease_id


def _hub(settings) -> WorkerHub:
    return WorkerHub(settings)


async def test_reconcile_keeps_reported_and_shares_ttl(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, lease_id = await _leased(store, worker_id)
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    try:
        revoke, ttl = await hub.reconcile_inventory(
            store, bus, worker_id, {session_id: lease_id}
        )
    finally:
        await bus.close()
    assert revoke == []
    assert str(session_id) in ttl
    assert hub.known_live_sessions() == [session_id]


async def test_reconcile_orphan_fails_and_clears_lease(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _leased(store, worker_id)
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    try:
        revoke, _ttl = await hub.reconcile_inventory(store, bus, worker_id, {})
    finally:
        await bus.close()
    assert revoke == []
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        assert row.lease_id is None
        events = await list_events(db, tenant_id, session_id)
    assert events[-1].type == "agent.session.error"


async def test_reconcile_unknown_session_is_revoked(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    ghost = uuid.uuid4()
    ghost_lease = uuid.uuid4()
    try:
        revoke, _ttl = await hub.reconcile_inventory(
            store, bus, worker_id, {ghost: ghost_lease}
        )
    finally:
        await bus.close()
    assert revoke == [
        {"session_id": str(ghost), "lease_id": str(ghost_lease)},
    ]


async def test_reconcile_mismatched_lease_is_revoked(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _leased(store, worker_id)
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    other_lease = uuid.uuid4()
    try:
        revoke, _ttl = await hub.reconcile_inventory(
            store, bus, worker_id, {session_id: other_lease}
        )
    finally:
        await bus.close()
    assert revoke == [
        {"session_id": str(session_id), "lease_id": str(other_lease)},
    ]


async def test_reconnect_to_another_replica_renews_lease(
    store: Store, settings
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id = await _leased(store, worker_id)
    first = _hub(settings)
    second = _hub(settings)
    bus = create_event_bus(settings, store=store)
    try:
        from apipi.workerhub.connection import WorkerConnection

        conn_a = WorkerConnection(
            worker_id=worker_id,
            generation=1,
            websocket=MagicMock(),
            capacity=4,
            memory_mb=8192,
            run_mode="none",
        )
        sessions = await first.restore_leases(conn_a, store, None)
        assert sessions[session_id] == 0
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            before = row.lease_until
        conn_b = WorkerConnection(
            worker_id=worker_id,
            generation=2,
            websocket=MagicMock(),
            capacity=4,
            memory_mb=8192,
            run_mode="none",
        )
        claimed = [
            {
                "session_id": session_id,
                "lease_id": lease_id,
                "last_seq": 0,
            }
        ]
        sessions = await second.restore_leases(conn_b, store, claimed)
        assert sessions[session_id] == 0
        assert lease_id in conn_b.leases
        revoke, _ttl = await second.reconcile_inventory(
            store, bus, worker_id, {session_id: lease_id}
        )
        assert revoke == []
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            after = row.lease_until
        assert after is not None and before is not None
        assert after >= before
    finally:
        await bus.close()


def test_command_dedupe_bounds_and_forgets() -> None:
    seen = CommandDedupe(limit=2)
    session_id = uuid.uuid4()
    assert seen.duplicate(session_id, "a") is False
    assert seen.duplicate(session_id, "a") is True
    assert seen.duplicate(session_id, "b") is False
    assert seen.duplicate(session_id, "c") is False
    assert seen.duplicate(session_id, "a") is False
    seen.forget(session_id)
    assert seen.duplicate(session_id, "b") is False


async def test_owned_sessions_filters_other_workers(store: Store, settings) -> None:
    from apipi.workerhub.connection import WorkerConnection

    worker_id = uuid.uuid4()
    other_id = uuid.uuid4()
    _t, session_id, lease_id = await _leased(store, worker_id)
    _t2, other_session, _other_lease = await _leased(store, other_id)
    hub = _hub(settings)
    conn = WorkerConnection(
        worker_id=worker_id,
        generation=1,
        websocket=MagicMock(),
        capacity=4,
        memory_mb=8192,
        run_mode="none",
    )
    conn.leases.add(lease_id)
    owned = await hub.owned_sessions(store, conn, [session_id, other_session])
    assert owned == [session_id]


async def test_reconcile_spares_orphan_with_command_in_flight(
    store: Store, settings
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, lease_id = await _leased(store, worker_id)
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    hub._unacked[lease_id] = {"id": str(uuid.uuid4()), "op": "turn.start"}
    try:
        revoke, _ttl = await hub.reconcile_inventory(store, bus, worker_id, {})
        assert revoke == []
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            assert row.lease_id == lease_id
            events = await list_events(db, tenant_id, session_id)
        assert all(event.type != "agent.session.error" for event in events)
        hub._unacked.pop(lease_id, None)
        revoke, _ttl = await hub.reconcile_inventory(store, bus, worker_id, {})
        assert revoke == []
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            assert row.lease_id is None
            events = await list_events(db, tenant_id, session_id)
        assert events[-1].type == "agent.session.error"
    finally:
        await bus.close()


async def test_takeover_renews_only_claimed_leases(store: Store, settings) -> None:
    from apipi.workerhub.connection import WorkerConnection

    worker_id = uuid.uuid4()
    tenant_a, first_id, first_lease = await _leased(store, worker_id)
    tenant_b, second_id, _second_lease = await _leased(store, worker_id)
    async with store.session() as db:
        stale = await get_session(db, tenant_b, second_id)
        assert stale is not None
        stale.lease_until = utc_now() - timedelta(seconds=5)
        await db.flush()
    hub = _hub(settings)
    conn = WorkerConnection(
        worker_id=worker_id,
        generation=1,
        websocket=MagicMock(),
        capacity=4,
        memory_mb=8192,
        run_mode="none",
    )
    claimed = [
        {"session_id": first_id, "lease_id": first_lease, "last_seq": 0},
    ]
    sessions = await hub.restore_leases(conn, store, claimed)
    assert set(sessions) == {first_id}
    assert first_lease in conn.leases
    async with store.session() as db:
        first = await get_session(db, tenant_a, first_id)
        second = await get_session(db, tenant_b, second_id)
        assert first is not None and second is not None
        assert first.lease_until is not None and second.lease_until is not None
        # Only the claimed lease moved: the unclaimed row keeps its stale
        # cursor instead of being renewed with it.
        assert first.lease_until > second.lease_until


async def test_reconcile_unleased_gets_ttl_while_leased(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _leased(store, worker_id)
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    try:
        revoke, ttl = await hub.reconcile_inventory(
            store, bus, worker_id, {}, [session_id]
        )
        assert revoke == []
        assert str(session_id) in ttl
    finally:
        await bus.close()


async def test_reconcile_unleased_unknown_is_revoked(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    ghost = uuid.uuid4()
    try:
        revoke, ttl = await hub.reconcile_inventory(store, bus, worker_id, {}, [ghost])
        assert revoke == [{"session_id": str(ghost)}]
        assert ttl == {}
    finally:
        await bus.close()


async def test_reconcile_unleased_released_session_gets_ttl_then_reaps(
    store: Store, settings
) -> None:
    from datetime import datetime
    from pathlib import Path
    from types import SimpleNamespace

    from apipi.store.repo import clear_session_lease
    from apipi.worker.inventory import _seed_reaper_ttl
    from apipi.worker.pi.artifacts import reap_workspaces
    from apipi.worker.pi.pool import PiPool

    worker_id = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "openai_hosted"}, metadata={}
        )
        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
        tenant_id, session_id = tenant.id, row.id
    # The Pi stopped so the lease is released, but the row (and the
    # workspace) intentionally stay until the workspace idle TTL.
    async with store.session() as db:
        await clear_session_lease(db, tenant_id, session_id)
    hub = _hub(settings)
    bus = create_event_bus(settings, store=store)
    try:
        revoke, ttl = await hub.reconcile_inventory(
            store, bus, worker_id, {}, [session_id]
        )
    finally:
        await bus.close()
    assert revoke == []
    entry = ttl[str(session_id)]
    assert isinstance(entry["idle_ttl_seconds"], (int, float))
    assert isinstance(entry["idle_since_epoch"], float)
    # Wire the answer through the worker seed: the row baseline
    # sticks instead of restarting on every reply.
    seeded: dict[str, tuple[float | None, float, str | None]] = {}
    _seed_reaper_ttl(SimpleNamespace(_context_ttl=seeded), ttl)
    assert seeded[str(session_id)][1] == entry["idle_since_epoch"]
    workspace = Path(str(settings.sessions_dir)) / str(tenant_id) / str(session_id)
    workspace.mkdir(parents=True)
    (workspace / "file.txt").write_text("x")
    pool = PiPool(settings)
    ttl_seconds = float(entry["idle_ttl_seconds"])
    since = entry["idle_since_epoch"]
    before = datetime.fromtimestamp(since + ttl_seconds - 1, tz=UTC)
    after = datetime.fromtimestamp(since + ttl_seconds + 1, tz=UTC)
    kept = await reap_workspaces(settings, pool, ttl_overrides=seeded, now=before)
    assert kept == []
    assert workspace.is_dir()
    wiped = await reap_workspaces(settings, pool, ttl_overrides=seeded, now=after)
    assert wiped == [str(session_id)]
    assert not workspace.exists()


def test_seed_reaper_ttl_never_moves_clock_backwards() -> None:
    import time
    from types import SimpleNamespace

    from apipi.worker.inventory import _seed_reaper_ttl

    session_id = str(uuid.uuid4())
    fresh = time.time()
    seeded: dict[str, tuple[float | None, float, str | None]] = {
        session_id: (60.0, fresh, "openai_hosted")
    }
    execution = SimpleNamespace(_context_ttl=seeded)
    # An older row touch must not rewind the worker's own fresher
    # turn-activity baseline.
    _seed_reaper_ttl(
        execution,
        {session_id: {"idle_ttl_seconds": 60, "idle_since_epoch": fresh - 120}},
    )
    assert seeded[session_id][1] == fresh
    # An unknown entry adopts the reply baseline instead.
    unknown = str(uuid.uuid4())
    since = fresh - 30
    _seed_reaper_ttl(
        execution,
        {unknown: {"idle_ttl_seconds": 60, "idle_since_epoch": since}},
    )
    assert seeded[unknown][1] == since
