import uuid
from datetime import UTC, timedelta

import pytest

from apipi.config import Settings
from apipi.errors import ApiError
from apipi.execution import RemoteExecution
from apipi.runtime import EventHub
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    extend_worker_leases,
    get_session,
    get_worker,
    set_session_lease,
    upsert_worker,
)


async def test_set_session_lease_is_conditional(store: Store) -> None:
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    lease_a = uuid.uuid4()
    lease_b = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session = await create_session(db, tenant.id)
        first = await set_session_lease(
            db,
            tenant.id,
            session.id,
            worker_id=worker_a,
            lease_id=lease_a,
            lease_until=utc_now() + timedelta(hours=1),
        )
        assert first is not None
        stolen = await set_session_lease(
            db,
            tenant.id,
            session.id,
            worker_id=worker_b,
            lease_id=lease_b,
            lease_until=utc_now() + timedelta(hours=1),
        )
        assert stolen is None
        row = await get_session(db, tenant.id, session.id)
        assert row is not None
        assert row.lease_id == lease_a
        assert row.worker_id == worker_a


async def test_set_session_lease_replaces_expired(store: Store) -> None:
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session = await create_session(db, tenant.id)
        await set_session_lease(
            db,
            tenant.id,
            session.id,
            worker_id=worker_a,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() - timedelta(seconds=1),
        )
        lease_b = uuid.uuid4()
        second = await set_session_lease(
            db,
            tenant.id,
            session.id,
            worker_id=worker_b,
            lease_id=lease_b,
            lease_until=utc_now() + timedelta(hours=1),
        )
        assert second is not None
        assert second.lease_id == lease_b
        assert second.worker_id == worker_b


async def test_extend_worker_leases_one_update(store: Store) -> None:
    worker_id = uuid.uuid4()
    until = utc_now() + timedelta(minutes=5)
    later = utc_now() + timedelta(minutes=30)
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session = await create_session(db, tenant.id)
        tenant_id = tenant.id
        session_id = session.id
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=until,
        )
        await extend_worker_leases(db, worker_id, lease_until=later)
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        got = row.lease_until
        assert got is not None
        if got.tzinfo is None:
            got = got.replace(tzinfo=UTC)
        assert got >= later.replace(tzinfo=UTC) - timedelta(seconds=2)


class _Hub:
    def __init__(self) -> None:
        self._ids: set[uuid.UUID] = set()

    def get(self, worker_id: uuid.UUID) -> object | None:
        return object() if worker_id in self._ids else None

    def live(self) -> int:
        return 1

    async def command(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def acquire(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


async def test_remote_execution_names_missing_socket_instance(
    store: Store, settings: Settings
) -> None:
    worker_id = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session = await create_session(db, tenant.id)
        await upsert_worker(db, worker_id, capacity=1, api_instance_id="node-b")
        await set_session_lease(
            db,
            tenant.id,
            session.id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(hours=1),
        )
        tenant_id = tenant.id
        session_id = session.id
    execution = RemoteExecution(settings, workers=_Hub(), store=store, hub=EventHub())
    with pytest.raises(ApiError) as exc:
        await execution.run_turn(tenant_id, session_id, "hi")
    assert exc.value.code == "capacity"
    assert exc.value.status_code == 429
    assert "node-b" in exc.value.message


async def test_upsert_worker_records_instance(store: Store) -> None:
    worker_id = uuid.uuid4()
    async with store.session() as db:
        await upsert_worker(db, worker_id, capacity=2, api_instance_id="node-a")
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.api_instance_id == "node-a"
        assert row.capacity == 2
