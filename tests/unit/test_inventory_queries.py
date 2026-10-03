import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import event

from apipi.services.event_bus import create_event_bus
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_agent,
    create_session,
    create_tenant,
    set_session_lease,
)
from apipi.workerhub.hub import WorkerHub


async def _seed(
    store: Store, worker_id: uuid.UUID, *, leased: int, unleased: int
) -> tuple[dict[uuid.UUID, uuid.UUID], list[uuid.UUID]]:
    reported: dict[uuid.UUID, uuid.UUID] = {}
    idle: list[uuid.UUID] = []
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        for index in range(leased + unleased):
            agent = await create_agent(
                db, tenant.id, name=f"a{index}", model="m", idle_ttl="2h"
            )
            row = await create_session(
                db, tenant.id, agent_id=agent.id, environment={"type": "none"}
            )
            if index < leased:
                lease_id = uuid.uuid4()
                await set_session_lease(
                    db,
                    tenant.id,
                    row.id,
                    worker_id=worker_id,
                    lease_id=lease_id,
                    lease_until=utc_now() + timedelta(seconds=30),
                )
                reported[row.id] = lease_id
            else:
                idle.append(row.id)
    return reported, idle


async def _selects(store: Store, settings: Any, leased: int, unleased: int) -> int:
    worker_id = uuid.uuid4()
    reported, idle = await _seed(store, worker_id, leased=leased, unleased=unleased)
    hub = WorkerHub(settings)
    bus = create_event_bus(settings, store=store)
    statements: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = store.engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        revoke, ttl = await hub.reconcile_inventory(
            store, bus, worker_id, reported, idle
        )
    finally:
        event.remove(engine, "before_cursor_execute", count)
        await bus.close()
    assert revoke == []
    assert len(ttl) == leased + unleased
    assert all(entry["idle_ttl_seconds"] == 7200 for entry in ttl.values())
    return len(statements)


async def test_inventory_ttl_lookup_does_not_grow_with_the_session_count(
    store: Store, settings: Any
) -> None:
    small = await _selects(store, settings, leased=1, unleased=1)
    large = await _selects(store, settings, leased=6, unleased=6)
    assert large == small
