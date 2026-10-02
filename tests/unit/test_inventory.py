"""Inventory reconcile and API-owned lease takeover (#449)."""

import uuid
from datetime import timedelta
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
from apipi.worker.hub import CommandDedupe, WorkerHub


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
        from apipi.worker.hub import WorkerConnection

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
    from apipi.worker.hub import WorkerConnection

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
