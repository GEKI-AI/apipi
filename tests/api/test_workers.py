import asyncio
import uuid
from datetime import timedelta
from typing import Any, cast

from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import Event, utc_now
from apipi.store.repo import get_session, get_worker


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


def _tenant(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def _worker_settings(
    settings: Settings,
    *,
    worker_lease_ttl: timedelta = timedelta(seconds=30),
    instance_id: str | None = None,
) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        worker_lease_ttl=worker_lease_ttl,
        instance_id=instance_id,
    )


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


async def test_worker_wrong_token(settings: Settings, store: Store) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, "nope")
    await worker.connect()
    hello = worker.hello
    assert hello is not None
    assert hello.get("error") == "unauthorized"
    await worker.close()


async def test_worker_register_lease_command_event_and_expiry(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    worker_settings = _worker_settings(settings)
    app = create_app(api_settings_for(worker_settings), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
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
            payload={"text": "hi"},
        )
        assert command is not None
        assert command["type"] == "command"
        assert command["op"] == "turn.start"
        assert command["payload"] == {"text": "hi", "run_mode": "none", "last_seq": 0}
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
                "type": "event",
                "lease_id": incoming["lease_id"],
                "event_type": "agent.session.error",
                "data": {"message": "from worker", "code": "worker_test"},
            }
        )
        events = await _wait_events(store, tenant_id, session_id, "agent.session.error")
        types = [event.type for event in events]
        assert "agent.session.error" in types
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


async def test_worker_reconnect_replays_unacked(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        session_id = uuid.UUID(created.json()["id"])
        first = FakeWorker(app, worker_secret, worker_id=str(uuid.uuid4()))
        hello = await first.connect()
        worker_id = hello["worker_id"]
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.cancel"
        )
        assert command is not None
        await first.receive_json()
        await first.close()
        second = FakeWorker(app, worker_secret, worker_id=worker_id)
        replayed_hello = await second.connect()
        assert replayed_hello["generation"] == 2
        replayed = await second.receive_json()
        assert replayed["id"] == command["id"]
        assert replayed["op"] == "turn.cancel"
        await second.close()


async def test_draining_worker_is_not_scheduled(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
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


async def test_worker_register_records_api_instance_id(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(
        api_settings_for(_worker_settings(settings, instance_id="node-a")),
        store=store,
    )
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(capacity=1)
    assert hello["ok"] is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    async with store.session() as db:
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.api_instance_id == "node-a"
    await worker.close()
    async with store.session() as db:
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.api_instance_id is None


async def test_worker_register_defaults_memory_mb(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(capacity=4)
    assert hello["ok"] is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    async with store.session() as db:
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.memory_mb == 4 * app.state.settings.microvm_mem_mib
    await worker.close()


async def test_worker_register_rejects_invalid_memory_mb(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(capacity=4, memory_mb=0)
    assert hello.get("ok") is False
    assert hello.get("error") == "invalid register"
    await worker.close()


async def test_worker_register_records_memory_mb(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
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


async def test_pick_skips_worker_at_ram_cap(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        session_id = uuid.UUID(created.json()["id"])
        full = FakeWorker(app, worker_secret)
        await full.connect(capacity=8, memory_mb=512)
        first = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert first is not None
        other = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        other_id = uuid.UUID(other.json()["id"])
        second = await app.state.workers.acquire(
            store, tenant_id, other_id, op="turn.start"
        )
        assert second is None
        roomy_secret = (await create_token(store, name="roomy")).secret
        roomy = FakeWorker(app, roomy_secret)
        await roomy.connect(capacity=1, memory_mb=4096)
        second = await app.state.workers.acquire(
            store, tenant_id, other_id, op="turn.start"
        )
        assert second is not None
        await roomy.close()
        await full.close()


async def test_pick_prefers_worker_with_more_free_ram(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        session_id = uuid.UUID(created.json()["id"])
        small = FakeWorker(app, worker_secret)
        small_hello = await small.connect(capacity=8, memory_mb=1024)
        large_secret = (await create_token(store, name="large")).secret
        large = FakeWorker(app, large_secret)
        large_hello = await large.connect(capacity=8, memory_mb=4096)
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert command is not None
        picked = app.state.workers.get(uuid.UUID(str(large_hello["worker_id"])))
        assert picked is not None
        assert uuid.UUID(command["lease_id"]) in picked.leases
        skipped = app.state.workers.get(uuid.UUID(str(small_hello["worker_id"])))
        assert skipped is not None
        assert not skipped.leases
        await small.close()
        await large.close()


async def test_worker_register_requires_run_mode(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(run_mode=None)
    assert hello.get("ok") is False
    assert hello.get("error") == "invalid register"
    await worker.close()


async def test_pick_keeps_none_and_microvm_apart(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        none_session = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        hosted_session = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "openai_hosted"},
            },
        )
        none_id = uuid.UUID(none_session.json()["id"])
        hosted_id = uuid.UUID(hosted_session.json()["id"])
        none_secret = (await create_token(store, name="none")).secret
        none_worker = FakeWorker(app, none_secret)
        none_hello = await none_worker.connect(
            capacity=4, run_mode="none", accepts=["none"]
        )
        microvm_secret = (await create_token(store, name="microvm")).secret
        microvm = FakeWorker(app, microvm_secret)
        microvm_hello = await microvm.connect(
            capacity=4, run_mode="microvm", accepts=["microvm"]
        )
        none_cmd = await app.state.workers.acquire(
            store, tenant_id, none_id, op="turn.start"
        )
        hosted_cmd = await app.state.workers.acquire(
            store, tenant_id, hosted_id, op="turn.start"
        )
        assert none_cmd is not None
        assert none_cmd["payload"]["run_mode"] == "none"
        assert hosted_cmd is not None
        assert hosted_cmd["payload"]["run_mode"] == "microvm"
        none_conn = app.state.workers.get(uuid.UUID(str(none_hello["worker_id"])))
        microvm_conn = app.state.workers.get(uuid.UUID(str(microvm_hello["worker_id"])))
        assert none_conn is not None
        assert microvm_conn is not None
        assert uuid.UUID(none_cmd["lease_id"]) in none_conn.leases
        assert uuid.UUID(hosted_cmd["lease_id"]) in microvm_conn.leases
        await none_worker.close()
        await microvm.close()


async def test_no_matching_worker_is_capacity(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        session_id = uuid.UUID(created.json()["id"])
        worker = FakeWorker(app, worker_secret)
        await worker.connect(capacity=2, run_mode="microvm", accepts=["microvm"])
        missed = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert missed is None
        await worker.close()


async def test_session_kind_is_ignored_for_placement(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    worker_settings = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
    )
    app = create_app(api_settings_for(worker_settings), store=store)
    token = "t"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "metadata": {"apipi.session_kind": "chat"},
            },
        )
        session_id = uuid.UUID(created.json()["id"])
        microvm = FakeWorker(app, worker_secret)
        await microvm.connect(capacity=4, run_mode="microvm", accepts=["microvm"])
        missed = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert missed is None
        none_secret = (await create_token(store, name="none")).secret
        none_worker = FakeWorker(app, none_secret)
        none_hello = await none_worker.connect(capacity=4, run_mode="none")
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert command is not None
        assert command["payload"]["run_mode"] == "none"
        conn = app.state.workers.get(uuid.UUID(str(none_hello["worker_id"])))
        assert conn is not None
        assert uuid.UUID(command["lease_id"]) in conn.leases
        await none_worker.close()
        await microvm.close()


async def _leased_session(
    app, client: AsyncClient, store: Store, worker: FakeWorker, token: str = "t"
) -> tuple[uuid.UUID, uuid.UUID, dict]:
    tenant_id = _tenant(token)
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    session_id = uuid.UUID(created.json()["id"])
    await worker.connect()
    command = await app.state.workers.acquire(
        store, tenant_id, session_id, op="turn.cancel"
    )
    assert command is not None
    await worker.receive_json()
    return tenant_id, session_id, command


async def test_hello_carries_the_api_heartbeat_interval(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(
        api_settings_for(
            _worker_settings(settings, worker_lease_ttl=timedelta(seconds=3))
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
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
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
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, first = await _leased_session(app, client, store, worker)
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
            _worker_settings(settings, worker_lease_ttl=timedelta(seconds=3))
        ),
        store=store,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, worker
        )

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


async def test_heartbeat_gap_is_measured_and_late_gaps_are_logged(
    settings: Settings, caplog
) -> None:
    import time

    from apipi.common.metrics import Metrics
    from apipi.workerhub.connection import WorkerConnection
    from apipi.workerhub.heartbeat import observe_heartbeat
    from apipi.workerhub.hub import WorkerHub

    metrics = Metrics()
    hub = WorkerHub(_worker_settings(settings), metrics=metrics)
    conn = WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=cast(Any, None),
        capacity=1,
        memory_mb=512,
        run_mode="none",
    )
    conn.connected_at = time.monotonic() - 20
    with caplog.at_level("WARNING", logger="apipi.worker"):
        observe_heartbeat(hub, conn)
        observe_heartbeat(hub, conn)
    late = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "worker.heartbeat.late"
    ]
    assert len(late) == 1
    assert late[0].source == "api"
    assert late[0].gap_seconds >= 20
    body = metrics.scrape().decode()
    assert "apipi_worker_heartbeat_gap_seconds_count 2.0" in body


async def test_lease_events_are_counted(
    settings: Settings, store: Store, worker_secret: str, caplog
) -> None:
    from tests.support.prom import metric_line

    app = create_app(
        api_settings_for(
            _worker_settings(settings).model_copy(update={"metrics": True})
        ),
        store=store,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, worker
        )
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
