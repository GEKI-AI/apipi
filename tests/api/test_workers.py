import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.errors import ApiError
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FakeHarness
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
        worker_token="worker-secret",
        worker_lease_ttl=worker_lease_ttl,
        instance_id=instance_id,
    )


async def test_worker_requires_token(settings: Settings, store: Store) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    worker = FakeWorker(app, "worker-secret")
    await worker.connect()
    hello = worker.hello
    assert hello is not None
    assert hello.get("ok") is False
    assert hello.get("error") == "unauthorized"
    await worker.close()


async def test_worker_wrong_token(settings: Settings, store: Store) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
    worker = FakeWorker(app, "nope")
    await worker.connect()
    hello = worker.hello
    assert hello is not None
    assert hello.get("error") == "unauthorized"
    await worker.close()


async def test_worker_register_lease_command_event_and_expiry(
    settings: Settings, store: Store
) -> None:
    worker_settings = _worker_settings(settings)
    app = create_app(worker_settings, store=store, harness=FakeHarness())
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
        worker = FakeWorker(app, "worker-secret")
        hello = await worker.connect(capacity=2)
        assert hello["ok"] is True
        assert hello["type"] == "hello"
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
        assert command["payload"] == {"text": "hi", "run_mode": "chat"}
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
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
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
        first = FakeWorker(app, "worker-secret", worker_id=str(uuid.uuid4()))
        hello = await first.connect()
        worker_id = hello["worker_id"]
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.cancel"
        )
        assert command is not None
        await first.receive_json()
        await first.close()
        second = FakeWorker(app, "worker-secret", worker_id=worker_id)
        replayed_hello = await second.connect()
        assert replayed_hello["generation"] == 2
        replayed = await second.receive_json()
        assert replayed["id"] == command["id"]
        assert replayed["op"] == "turn.cancel"
        await second.close()


async def test_draining_worker_is_not_scheduled(
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
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
        draining = FakeWorker(app, "worker-secret")
        hello = await draining.connect(capacity=4)
        assert hello.get("ok") is True
        await draining.send_json({"type": "heartbeat", "drain": True})
        for _ in range(50):
            if app.state.workers.pick(run_mode="chat") is None:
                break
            await asyncio.sleep(0.02)
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert command is None
        ready = FakeWorker(app, "worker-secret")
        await ready.connect(capacity=1)
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert command is not None
        await ready.close()
        await draining.close()


async def test_worker_register_records_api_instance_id(
    settings: Settings, store: Store
) -> None:
    app = create_app(
        _worker_settings(settings, instance_id="node-a"),
        store=store,
        harness=FakeHarness(),
    )
    worker = FakeWorker(app, "worker-secret")
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
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
    worker = FakeWorker(app, "worker-secret")
    hello = await worker.connect(capacity=4)
    assert hello["ok"] is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    async with store.session() as db:
        row = await get_worker(db, worker_id)
        assert row is not None
        assert row.memory_mb == 4 * app.state.settings.microvm_mem_mib
    await worker.close()


async def test_worker_register_rejects_invalid_memory_mb(
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
    worker = FakeWorker(app, "worker-secret")
    hello = await worker.connect(capacity=4, memory_mb=0)
    assert hello.get("ok") is False
    assert hello.get("error") == "invalid register"
    await worker.close()


async def test_worker_register_records_memory_mb(
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
    worker = FakeWorker(app, "worker-secret")
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


async def test_pick_skips_worker_at_ram_cap(settings: Settings, store: Store) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
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
        full = FakeWorker(app, "worker-secret")
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
        roomy = FakeWorker(app, "worker-secret")
        await roomy.connect(capacity=1, memory_mb=4096)
        second = await app.state.workers.acquire(
            store, tenant_id, other_id, op="turn.start"
        )
        assert second is not None
        await roomy.close()
        await full.close()


async def test_pick_prefers_worker_with_more_free_ram(
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
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
        small = FakeWorker(app, "worker-secret")
        small_hello = await small.connect(capacity=8, memory_mb=1024)
        large = FakeWorker(app, "worker-secret")
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
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
    worker = FakeWorker(app, "worker-secret")
    hello = await worker.connect(run_mode=None)
    assert hello.get("ok") is False
    assert hello.get("error") == "invalid register"
    await worker.close()


async def test_pick_keeps_chat_and_microvm_apart(
    settings: Settings, store: Store
) -> None:
    app = create_app(_worker_settings(settings), store=store, harness=FakeHarness())
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
        chat = FakeWorker(app, "worker-secret")
        chat_hello = await chat.connect(capacity=4, run_mode="chat")
        microvm = FakeWorker(app, "worker-secret")
        microvm_hello = await microvm.connect(capacity=4, run_mode="microvm")
        none_cmd = await app.state.workers.acquire(
            store, tenant_id, none_id, op="turn.start"
        )
        hosted_cmd = await app.state.workers.acquire(
            store, tenant_id, hosted_id, op="turn.start"
        )
        assert none_cmd is not None
        assert none_cmd["payload"]["run_mode"] == "chat"
        assert hosted_cmd is not None
        assert hosted_cmd["payload"]["run_mode"] == "microvm"
        chat_conn = app.state.workers.get(uuid.UUID(str(chat_hello["worker_id"])))
        microvm_conn = app.state.workers.get(uuid.UUID(str(microvm_hello["worker_id"])))
        assert chat_conn is not None
        assert microvm_conn is not None
        assert uuid.UUID(none_cmd["lease_id"]) in chat_conn.leases
        assert uuid.UUID(hosted_cmd["lease_id"]) in microvm_conn.leases
        await chat.close()
        await microvm.close()


async def test_env_none_reject_is_placement_error(
    settings: Settings, store: Store
) -> None:
    worker_settings = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        worker_token="worker-secret",
        env_none_placement="reject",
    )
    app = create_app(worker_settings, store=store, harness=FakeHarness())
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
        worker = FakeWorker(app, "worker-secret")
        await worker.connect(capacity=2, run_mode="chat")
        with pytest.raises(ApiError) as exc:
            await app.state.workers.acquire(
                store, tenant_id, session_id, op="turn.start"
            )
        assert exc.value.code == "placement"
        assert exc.value.status_code == 400
        await worker.close()


async def test_session_kind_chat_ignores_env_none_microvm(
    settings: Settings, store: Store
) -> None:
    worker_settings = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        worker_token="worker-secret",
        env_none_placement="microvm",
    )
    app = create_app(worker_settings, store=store, harness=FakeHarness())
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
        microvm = FakeWorker(app, "worker-secret")
        await microvm.connect(capacity=4, run_mode="microvm")
        missed = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert missed is None
        chat = FakeWorker(app, "worker-secret")
        chat_hello = await chat.connect(capacity=4, run_mode="chat")
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        assert command is not None
        assert command["payload"]["run_mode"] == "chat"
        conn = app.state.workers.get(uuid.UUID(str(chat_hello["worker_id"])))
        assert conn is not None
        assert uuid.UUID(command["lease_id"]) in conn.leases
        await chat.close()
        await microvm.close()
