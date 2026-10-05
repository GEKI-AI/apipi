"""Inventory reconcile and sandbox seen over the worker socket (#449)."""

import uuid
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    get_session,
    set_session_lease,
)


def _worker_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
    )


async def _hosted_lease(
    store: Store, worker_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "openai_hosted"}, metadata={}
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
        tenant_id, session_id = tenant.id, row.id
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        row.sandbox_state = "ready"
        row.sandbox_seen_at = utc_now() - timedelta(seconds=60)
    return tenant_id, session_id, lease_id


async def _reply(worker: FakeWorker) -> dict[str, Any]:
    while True:
        message = await worker.receive_json(timeout=10)
        if message.get("type") == "inventory.reply":
            return message


async def test_inventory_reply_revokes_unknown_and_shares_ttl(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    assert hello.get("ok") is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    _tenant, session_id, lease_id = await _hosted_lease(store, worker_id)
    ghost = uuid.uuid4()
    await worker.send_json(
        {
            "type": "inventory",
            "sessions": [
                {
                    "session_id": str(session_id),
                    "lease_id": str(lease_id),
                    "last_seq": 4,
                },
                {
                    "session_id": str(ghost),
                    "lease_id": str(uuid.uuid4()),
                    "last_seq": 0,
                },
            ],
        }
    )
    reply = await _reply(worker)
    assert reply["revoke"] == [
        {
            "type": "lease.revoke",
            "session_id": str(ghost),
            "lease_id": reply["revoke"][0]["lease_id"],
        }
    ]
    assert reply["ttl"][str(session_id)]["env_type"] == "openai_hosted"
    assert isinstance(reply["ttl"][str(session_id)]["idle_ttl_seconds"], (int, float))
    await worker.close()


async def test_inventory_clears_orphaned_lease(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    try:
        hello = await worker.connect()
        worker_id = uuid.UUID(str(hello["worker_id"]))
        tenant_id, session_id, _lease = await _hosted_lease(store, worker_id)
        await worker.send_json({"type": "inventory", "sessions": []})
        reply = await _reply(worker)
        assert reply["revoke"] == []
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            assert row.lease_id is None
            events = await list_events(db, tenant_id, session_id)
        assert events[-1].type == "agent.session.error"
    finally:
        await worker.close()


async def test_hello_carries_revoke_and_ttl(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app: FastAPI = create_app(api_settings_for(_worker_settings(settings)), store=store)
    first = FakeWorker(app, worker_secret)
    hello = await first.connect()
    worker_id = str(hello["worker_id"])
    await first.close()
    ghost = uuid.uuid4()
    _tenant, session_id, lease_id = await _hosted_lease(store, uuid.UUID(worker_id))
    second = FakeWorker(app, worker_secret, worker_id=worker_id)
    await second.ws.connect()
    await second.ws.send_json(
        {
            "type": "register",
            "protocol": 2,
            "id": worker_id,
            "capacity": 1,
            "run_mode": "none",
            "accepts": ["none"],
            "running": [
                {
                    "session_id": str(session_id),
                    "lease_id": str(lease_id),
                    "last_seq": 0,
                },
                {
                    "session_id": str(ghost),
                    "lease_id": str(uuid.uuid4()),
                    "last_seq": 0,
                },
            ],
        }
    )
    hello2 = await second.ws.receive_json(timeout=10)
    assert hello2.get("ok") is True
    assert hello2["sessions"] == {str(session_id): 0}
    assert [entry["session_id"] for entry in hello2["revoke"]] == [str(ghost)]
    assert str(session_id) in hello2["ttl"]
    await second.close()


async def test_sandbox_seen_touches_only_owned(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    first = FakeWorker(app, worker_secret)
    hello = await first.connect()
    worker_id = uuid.UUID(str(hello["worker_id"]))
    await first.close()
    _tenant, session_id, lease_id = await _hosted_lease(store, worker_id)
    worker = FakeWorker(app, worker_secret, worker_id=str(worker_id))
    await worker.ws.connect()
    await worker.ws.send_json(
        {
            "type": "register",
            "protocol": 2,
            "id": str(worker_id),
            "capacity": 1,
            "run_mode": "none",
            "accepts": ["none"],
            "running": [
                {
                    "session_id": str(session_id),
                    "lease_id": str(lease_id),
                    "last_seq": 0,
                }
            ],
        }
    )
    hello2 = await worker.ws.receive_json(timeout=10)
    assert hello2.get("ok") is True
    async with store.session() as db:
        row = await get_session(db, _tenant, session_id)
        assert row is not None
        before = row.sandbox_seen_at
    stranger = uuid.uuid4()
    await worker.send_json(
        {"type": "sandbox.seen", "session_ids": [str(session_id), str(stranger)]}
    )
    await worker.send_json({"type": "inventory", "sessions": []})
    await _reply(worker)
    async with store.session() as db:
        row = await get_session(db, _tenant, session_id)
        assert row is not None
        after = row.sandbox_seen_at
    assert before is not None and after is not None
    assert after > before
    await worker.close()


async def test_inventory_unleased_gets_ttl_or_revoke(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    assert hello.get("ok") is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    _tenant, session_id, _lease = await _hosted_lease(store, worker_id)
    ghost = uuid.uuid4()
    await worker.send_json(
        {
            "type": "inventory",
            "sessions": [
                {"session_id": str(session_id), "last_seq": 0},
                {"session_id": str(ghost), "last_seq": 0},
            ],
        }
    )
    reply = await _reply(worker)
    assert reply["ttl"][str(session_id)]["env_type"] == "openai_hosted"
    assert reply["revoke"] == [
        {"type": "lease.revoke", "session_id": str(ghost)},
    ]
    await worker.close()
