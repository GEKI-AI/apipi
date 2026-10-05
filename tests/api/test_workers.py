import asyncio
import uuid
from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from tests.support.config import none_settings_for
from tests.support.fake_worker import FakeWorker, acquire_lease
from tests.support.http import auth, tenant_of
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import Event, utc_now
from apipi.store.repo import get_session, get_worker


async def _wait_events(
    store: Store,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_type: str,
) -> list[Event]:
    for _ in range(50):
        async with store.session() as db:
            events = await list_events(db, tenant_id, session_id)
        if any(event.type == event_type for event in events):
            return events
        await asyncio.sleep(0.02)
    async with store.session() as db:
        return await list_events(db, tenant_id, session_id)


async def test_worker_requires_token(settings: Settings, store: Store) -> None:
    app = create_app(api_settings_for(settings), store=store)
    worker = FakeWorker(app, "no-such-token")
    await worker.connect()
    hello = worker.hello
    assert hello is not None
    assert hello.get("ok") is False
    assert hello.get("error") == "unauthorized"
    closed = await worker.wait_close()
    assert closed["code"] == 1008
    await worker.close()


async def test_worker_register_lease_command_event_and_expiry(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    worker_settings = none_settings_for(settings)
    app = create_app(api_settings_for(worker_settings), store=store)
    token = "t"
    tenant_id = tenant_of(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        session_id = uuid.UUID(created.json()["id"])
        worker = FakeWorker(app, worker_secret)
        hello = await worker.connect(capacity=2)
        assert hello["ok"] is True
        assert hello["type"] == "hello"
        assert hello["lease_ttl_seconds"] == 30
        assert hello["heartbeat_seconds"] == 10
        command = await app.state.workers.acquire(
            store,
            tenant_id,
            session_id,
            op="turn.start",
            payload={"text": "hi", "tenant_id": tenant_id},
        )
        assert command is not None
        assert command["type"] == "command"
        assert command["op"] == "turn.start"
        assert command["payload"] == {
            "text": "hi",
            "tenant_id": str(tenant_id),
            "run_mode": "none",
            "last_seq": 0,
        }
        incoming = await worker.receive_json()
        assert incoming["id"] == command["id"]
        assert incoming["lease_id"] == command["lease_id"]
        await worker.send_json(
            {
                "type": "lease.ack",
                "id": incoming["id"],
                "lease_id": incoming["lease_id"],
            }
        )
        await worker.send_json(
            {
                "v": 2,
                "session_id": str(session_id),
                "turn_id": None,
                "seq": 1,
                "type": "error",
                "payload": {"message": "from worker", "code": "worker_test"},
            }
        )
        events = await _wait_events(store, tenant_id, session_id, "agent.session.error")
        types = [event.type for event in events]
        assert "agent.session.error" in types
        ack = await worker.receive_json()
        assert ack["type"] == "ack" and ack["last_seq"] == 1
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.lease_until = utc_now() - timedelta(seconds=1)
            await db.flush()
        expired = await app.state.workers.expire(store, app.state.event_hub)
        assert session_id in expired
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            assert row.lease_id is None
            events = await list_events(db, tenant_id, session_id)
        codes = [
            event.data.get("code")
            for event in events
            if event.type == "agent.session.error"
        ]
        assert "worker_lease_expired" in codes
        revoke = await worker.receive_json()
        assert revoke["type"] == "lease.revoke"
        await worker.close()


async def test_draining_worker_is_not_scheduled(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(none_settings_for(settings)), store=store)
    token = "t"
    tenant_id = tenant_of(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        session_id = uuid.UUID(created.json()["id"])
        draining = FakeWorker(app, worker_secret)
        hello = await draining.connect(capacity=4)
        assert hello.get("ok") is True
        await draining.send_json({"type": "heartbeat", "drain": True})
        for _ in range(50):
            if app.state.workers.pick(kind="none") is None:
                break
            await asyncio.sleep(0.02)
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert command is None
        ready_secret = (await create_token(store, name="ready")).secret
        ready = FakeWorker(app, ready_secret)
        await ready.connect(capacity=1)
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert command is not None
        await ready.close()
        await draining.close()


async def test_worker_register_defaults_memory_mb(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(none_settings_for(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(capacity=4)
    assert hello["ok"] is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    async with store.session() as db:
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.memory_mb == 4 * app.state.settings.microvm_mem_mib
    await worker.close()


async def test_worker_register_records_memory_mb(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(none_settings_for(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(capacity=4, memory_mb=2048)
    assert hello["ok"] is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    async with store.session() as db:
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.capacity == 4
        assert row.memory_mb == 2048
    conn = app.state.workers.get(worker_id)
    assert conn is not None
    assert conn.memory_mb == 2048
    await worker.send_json({"type": "heartbeat", "memory_mb": 8192, "capacity": 4})
    for _ in range(50):
        if conn.memory_mb == 8192:
            break
        await asyncio.sleep(0.02)
    assert conn.memory_mb == 8192
    async with store.session() as db:
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.memory_mb == 8192
    await worker.close()


async def test_hello_carries_the_api_heartbeat_interval(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(
        api_settings_for(
            none_settings_for(settings, worker_lease_ttl=timedelta(seconds=3))
        ),
        store=store,
    )
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    assert hello["lease_ttl_seconds"] == 3
    assert hello["heartbeat_seconds"] == 1
    await worker.close()


async def test_commands_carry_the_session_cursor(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(none_settings_for(settings)), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await acquire_lease(
            app, client, store, worker
        )
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.worker_seq = 7
            await db.flush()
        follow = await app.state.workers.command(
            store, tenant_id, session_id, op="turn.start", payload={"text": "again"}
        )
        assert follow is not None
        assert follow["payload"]["last_seq"] == 7
        cancel = await app.state.workers.command(
            store, tenant_id, session_id, op="turn.cancel"
        )
        assert cancel is not None
        assert "last_seq" not in cancel["payload"]
        await worker.close()


async def test_new_lease_command_carries_the_persisted_cursor(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(none_settings_for(settings)), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, first = await acquire_lease(app, client, store, worker)
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.worker_seq = 41
            await db.flush()
        await app.state.workers.release(
            store, tenant_id, session_id, uuid.UUID(first["lease_id"])
        )
        second = await app.state.workers.acquire(
            store,
            tenant_id,
            session_id,
            op="turn.start",
            payload={"text": "next"},
        )
        assert second is not None
        assert second["payload"]["last_seq"] == 41
        await worker.close()


async def test_ack_and_ingest_renew_the_lease_once_per_interval(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(
        api_settings_for(
            none_settings_for(settings, worker_lease_ttl=timedelta(seconds=3))
        ),
        store=store,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await acquire_lease(app, client, store, worker)

        async def lease_until():
            async with store.session() as db:
                row = await get_session(db, tenant_id, session_id)
                assert row is not None
                return row.lease_until

        conn = app.state.workers.get(uuid.UUID(str(worker.worker_id)))
        assert conn is not None
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.lease_until = utc_now() + timedelta(milliseconds=500)
            await db.flush()
        before = await lease_until()
        conn.last_renewed = 0.0
        await worker.send_json(
            {
                "type": "lease.ack",
                "id": command["id"],
                "lease_id": command["lease_id"],
            }
        )
        for _ in range(50):
            after = await lease_until()
            if after != before:
                break
            await asyncio.sleep(0.02)
        assert after is not None and before is not None
        assert after > before
        renewed_at = conn.last_renewed
        await worker.send_json(
            {
                "type": "lease.ack",
                "id": command["id"],
                "lease_id": command["lease_id"],
            }
        )
        await asyncio.sleep(0.1)
        assert conn.last_renewed == renewed_at
        await worker.close()


async def test_lease_events_are_counted(
    settings: Settings, store: Store, worker_secret: str, caplog
) -> None:
    from tests.support.prom import metric_line

    app = create_app(
        api_settings_for(
            none_settings_for(settings).model_copy(update={"metrics": True})
        ),
        store=store,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await acquire_lease(app, client, store, worker)
        await worker.send_json({"type": "heartbeat"})
        for _ in range(50):
            body = app.state.metrics.scrape().decode()
            if 'apipi_worker_lease_events_total{event="renewed"}' in body:
                break
            await asyncio.sleep(0.02)
        assert metric_line(
            body, "apipi_worker_lease_events_total", event="renewed"
        ).endswith(" 1.0")
        await worker.send_json(
            {
                "type": "lease.release",
                "session_id": str(session_id),
                "lease_id": command["lease_id"],
            }
        )
        for _ in range(50):
            body = app.state.metrics.scrape().decode()
            if 'apipi_worker_lease_events_total{event="released"}' in body:
                break
            await asyncio.sleep(0.02)
        assert metric_line(
            body, "apipi_worker_lease_events_total", event="released"
        ).endswith(" 1.0")
        again = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.cancel"
        )
        assert again is not None
        await worker.receive_json()
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.lease_until = utc_now() - timedelta(seconds=5)
            await db.flush()
        with caplog.at_level("ERROR", logger="apipi.worker"):
            await app.state.workers.expire(store, app.state.event_hub)
        body = app.state.metrics.scrape().decode()
        assert metric_line(
            body, "apipi_worker_lease_events_total", event="expired"
        ).endswith(" 1.0")
        expired = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "worker.lease.expired"
        ]
        assert len(expired) == 1
        assert expired[0].lease_ttl_seconds == 30
        assert expired[0].last_renewal_age_seconds >= 30
        assert expired[0].worker_connected is True
        await worker.close()
