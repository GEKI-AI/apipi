import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from tests.api.test_workers import _leased_session, _worker_settings
from tests.support.fake_worker import FakeWorker
from tests.support.prom import metric_line
from tests.support.split_worker import api_settings_for

from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.blobs import MemoryStore
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import get_session, get_worker, upsert_worker
from apipi.workerhub import serve as serve_module
from apipi.workerhub.writer import ConnectionWriter


def _settings(
    settings: Settings, ttl: timedelta = timedelta(seconds=30), **kw: Any
) -> Settings:
    return api_settings_for(
        _worker_settings(settings, worker_lease_ttl=ttl, **kw).model_copy(
            update={"metrics": True}
        )
    )


async def _lease_until(store: Store, tenant_id: uuid.UUID, session_id: uuid.UUID):
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        return row.lease_until


async def _soon(store: Store, tenant_id: uuid.UUID, session_id: uuid.UUID):
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        row.lease_until = utc_now() + timedelta(milliseconds=500)
        await db.flush()
    return await _lease_until(store, tenant_id, session_id)


async def _extended(
    store: Store, tenant_id: uuid.UUID, session_id: uuid.UUID, before: Any
) -> bool:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        after = await _lease_until(store, tenant_id, session_id)
        if after is not None and after > before:
            return True
        await asyncio.sleep(0.01)
    return False


async def _until(check: Callable[[], bool], timeout: float = 5) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _status_envelope(session_id: uuid.UUID, seq: int) -> dict[str, Any]:
    return {
        "v": 2,
        "session_id": str(session_id),
        "seq": seq,
        "type": "session.status",
        "payload": {"status": "idle"},
    }


def _presign(session_id: uuid.UUID, seq: int) -> dict[str, Any]:
    return {
        "v": 2,
        "session_id": str(session_id),
        "seq": seq,
        "type": "artifact.presign",
        "payload": {
            "request_id": str(uuid.uuid4()),
            "kind": "artifact",
            "filename": "out.txt",
            "content_type": "text/plain",
            "size": 5,
        },
    }


async def test_heartbeats_renew_leases_while_ingest_is_stuck(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    real = serve_module.flush_batch

    async def stuck(*args: Any, **kwargs: Any) -> Any:
        await gate.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(serve_module, "flush_batch", stuck)
    app = create_app(_settings(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        await worker.send_json(_status_envelope(session_id, 1))
        await asyncio.sleep(0.05)
        before = await _soon(store, tenant_id, session_id)
        await worker.send_json({"type": "heartbeat"})
        assert await _extended(store, tenant_id, session_id, before)
        gate.set()
        ack = await worker.receive_json()
        assert ack["type"] == "ack"
        await worker.close()


class _SlowStore(MemoryStore):
    def __init__(self, release: asyncio.Event) -> None:
        super().__init__()
        self.release = release
        self.started = asyncio.Event()

    async def used_bytes(self, namespace: Any, prefix: str) -> int:
        self.started.set()
        await self.release.wait()
        return 0


async def test_heartbeats_renew_leases_while_the_object_store_is_slow(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    release = asyncio.Event()
    slow = _SlowStore(release)
    app = create_app(_settings(settings), store=store, objects=slow)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        await worker.send_json(_presign(session_id, 1))
        await asyncio.wait_for(slow.started.wait(), timeout=2)
        before = await _soon(store, tenant_id, session_id)
        await worker.send_json({"type": "heartbeat"})
        assert await _extended(store, tenant_id, session_id, before)
        release.set()
        kinds = {(await worker.receive_json())["type"] for _ in range(2)}
        assert kinds == {"ack", "artifact.presign.reply"}
        await worker.close()


async def test_a_failed_message_does_not_close_the_socket(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    calls = 0
    handled = 0
    real = serve_module.heartbeat_worker

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls, handled
        calls += 1
        if calls == 1:
            raise OperationalError("select", {}, Exception("connection reset"))
        result = await real(*args, **kwargs)
        handled += 1
        return result

    monkeypatch.setattr(serve_module, "heartbeat_worker", flaky)
    app = create_app(_settings(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        for frame in (
            {"type": "websocket.receive", "text": "{not json"},
            {"type": "websocket.receive", "bytes": b"\x00\x01"},
            {"type": "websocket.receive", "text": "[1, 2]"},
        ):
            await worker.ws._incoming.put(frame)
        await worker.send_json({"type": "heartbeat"})
        await worker.send_json({"type": "heartbeat"})
        assert await _until(lambda: handled == 1)
        before = await _soon(store, tenant_id, session_id)
        await worker.send_json({"type": "heartbeat"})
        assert await _until(lambda: handled == 2)
        assert await _lease_until(store, tenant_id, session_id) > before
        assert calls == 3
        conn = app.state.workers.get(uuid.UUID(str(worker.worker_id)))
        assert conn is not None
        assert conn.disconnect_reason is None
        body = app.state.metrics.scrape().decode()
        assert metric_line(
            body, "apipi_worker_protocol_total", event="frame_invalid"
        ).endswith(" 3.0")
        assert metric_line(
            body, "apipi_worker_protocol_total", event="message_failed"
        ).endswith(" 1.0")
        failures = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "worker.message.failed"
        ]
        assert len(failures) == 1
        assert failures[0].__dict__["transient"] is True
        await worker.close()


async def test_a_release_is_retried_after_a_transient_error(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_module, "RETRY_DELAYS", (0.01, 0.01, 0.01))
    app = create_app(_settings(settings), store=store)
    real = app.state.workers.release
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("update", {}, Exception("deadlock"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(app.state.workers, "release", flaky)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, worker
        )
        await worker.send_json(
            {
                "type": "lease.release",
                "session_id": str(session_id),
                "lease_id": command["lease_id"],
            }
        )
        for _ in range(100):
            async with store.session() as db:
                row = await get_session(db, tenant_id, session_id)
                assert row is not None
                if row.lease_id is None:
                    break
            await asyncio.sleep(0.01)
        assert row.lease_id is None
        assert calls == 2
        await worker.close()


async def test_a_connection_is_not_pickable_before_hello_is_queued(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(settings), store=store)
    hub = app.state.workers
    seen: list[bool] = []
    real_submit = ConnectionWriter.submit

    def submit(self: ConnectionWriter, payload: dict[str, Any]) -> Any:
        if payload.get("type") == "hello":
            seen.append(hub.pick(kind="none") is None and not hub._conns)
        return real_submit(self, payload)

    monkeypatch.setattr(ConnectionWriter, "submit", submit)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    assert hello["type"] == "hello"
    assert seen == [True]
    assert hub.pick(kind="none") is not None
    await worker.close()


async def test_reconnect_keeps_the_api_instance_id(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(_settings(settings, instance_id="api-1"), store=store)
    first = FakeWorker(app, worker_secret)
    await first.connect()
    second = FakeWorker(app, worker_secret, worker_id=first.worker_id)
    await second.connect()
    closed = await first.wait_close()
    assert closed["code"] == 1000
    worker_id = uuid.UUID(str(first.worker_id))
    for _ in range(100):
        body = app.state.metrics.scrape().decode()
        if 'reason="takeover"' in body:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    async with store.session() as db:
        row = await get_worker(db, worker_id)
    assert row is not None
    assert row.api_instance_id is not None
    assert row.api_instance_id.startswith("api-1-")
    conn = app.state.workers.get(worker_id)
    assert conn is not None
    assert second.hello is not None
    assert conn.connection_id == second.hello["connection_id"]
    await second.close()
    for _ in range(100):
        async with store.session() as db:
            row = await get_worker(db, worker_id)
        assert row is not None
        if row.api_instance_id is None:
            break
        await asyncio.sleep(0.01)
    assert row.api_instance_id is None


async def test_a_superseded_connection_cannot_renew_leases(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(_settings(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        async with store.session() as db:
            await upsert_worker(
                db,
                uuid.UUID(str(worker.worker_id)),
                capacity=1,
                memory_mb=512,
                api_instance_id="elsewhere",
            )
        before = await _soon(store, tenant_id, session_id)
        await worker.send_json({"type": "heartbeat"})
        closed = await worker.wait_close()
        assert closed["code"] == 1000
        assert await _lease_until(store, tenant_id, session_id) == before
        async with store.session() as db:
            row = await get_worker(db, uuid.UUID(str(worker.worker_id)))
        assert row is not None
        assert row.api_instance_id == "elsewhere"
        await worker.close()


async def test_invalid_optional_heartbeat_fields_are_ignored_and_the_lease_extends(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    handled = 0
    real = serve_module.heartbeat_worker

    async def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal handled
        result = await real(*args, **kwargs)
        handled += 1
        return result

    monkeypatch.setattr(serve_module, "heartbeat_worker", counted)
    app = create_app(_settings(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        conn = app.state.workers.get(uuid.UUID(str(worker.worker_id)))
        assert conn is not None
        capacity = conn.capacity
        for count, bad in enumerate(
            (
                {"capacity": 0},
                {"memory_mb": "lots"},
                {"run_mode": "  "},
                {"capacity": -3, "run_mode": 7, "drain": True},
            ),
            start=1,
        ):
            before = await _soon(store, tenant_id, session_id)
            await worker.send_json({"type": "heartbeat", **bad})
            assert await _until(lambda count=count: handled == count), bad
            assert await _lease_until(store, tenant_id, session_id) > before, bad
        assert conn.capacity == capacity
        assert conn.run_mode == "none"
        assert conn.draining is True
        assert any(
            getattr(record, "event", None) == "worker.heartbeat.field_ignored"
            for record in caplog.records
        )
        await worker.close()


async def test_expire_sends_revokes_after_commit_and_survives_a_stuck_socket(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apipi.workerhub import hub as hub_module

    monkeypatch.setattr(hub_module, "REVOKE_TIMEOUT", 0.1)
    app = create_app(_settings(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, worker
        )
        hub = app.state.workers
        conn = hub.get(uuid.UUID(str(worker.worker_id)))
        assert conn is not None
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.lease_until = utc_now() - timedelta(seconds=1)
            await db.flush()
        opened = 0
        real_session = store.session

        def counting() -> Any:
            nonlocal opened
            opened += 1
            return real_session()

        async def stuck(self: Any, wire: dict[str, Any]) -> None:
            assert wire["type"] == "lease.revoke"
            assert opened == 1
            await asyncio.Event().wait()

        monkeypatch.setattr(store, "session", counting)
        monkeypatch.setattr(type(conn), "send", stuck)
        expired = await hub.expire(store, app.state.event_hub)
        assert expired == [session_id]
        assert uuid.UUID(command["lease_id"]) not in conn.leases
        assert any(
            line
            for line in app.state.metrics.scrape().decode().splitlines()
            if line.startswith("apipi_worker_protocol_total")
            and 'event="revoke_failed"' in line
        )
        await worker.close()


async def test_expire_changes_nothing_in_memory_when_the_commit_fails(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apipi.workerhub import hub as hub_module

    app = create_app(_settings(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, worker
        )
        hub = app.state.workers
        conn = hub.get(uuid.UUID(str(worker.worker_id)))
        assert conn is not None
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.lease_until = utc_now() - timedelta(seconds=1)
            await db.flush()

        async def broken(*_args: Any, **_kwargs: Any) -> None:
            raise OperationalError("insert", {}, Exception("connection reset"))

        monkeypatch.setattr(hub_module, "persist_event", broken)
        with pytest.raises(OperationalError):
            await hub.expire(store, app.state.event_hub)
        assert uuid.UUID(command["lease_id"]) in conn.leases
        monkeypatch.undo()
        assert await hub.expire(store, app.state.event_hub) == [session_id]
        revoke = await worker.receive_json()
        assert revoke["type"] == "lease.revoke"
        await worker.close()


async def test_the_lease_reaper_loop_survives_a_failed_round(
    settings: Settings, store: Store
) -> None:
    from apipi.common.background import run_loop

    app = create_app(_settings(settings), store=store)
    gateway = app.state.gateway
    calls = 0

    async def flaky(*_args: Any) -> list[uuid.UUID]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("select", {}, Exception("failover"))
        return []

    gateway.workers.expire = flaky
    metrics: Metrics = app.state.metrics
    await run_loop(
        "lease_reaper_test",
        gateway._expire_worker_leases,
        interval=0,
        metrics=metrics,
        rounds=3,
    )
    assert calls == 3
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_background_loop_errors_total", loop="lease_reaper_test"
    ).endswith(" 1.0")


async def _closed_on_cancel(entered: asyncio.Event) -> None:
    entered.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise ValueError("Connection closed") from None


async def test_a_cancel_ends_the_socket_when_a_handler_turns_it_into_an_error(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def heartbeat(*_args: Any, **_kwargs: Any) -> bool:
        await _closed_on_cancel(entered)
        return True

    monkeypatch.setattr(serve_module, "heartbeat_worker", heartbeat)
    app = create_app(_settings(settings), store=store)
    worker = FakeWorker(app, worker_secret)
    await worker.connect()
    await worker.send_json({"type": "heartbeat"})
    await asyncio.wait_for(entered.wait(), timeout=5)
    task = worker.ws._task
    assert task is not None
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=5)
    assert task in done
    assert app.state.workers.get(uuid.UUID(str(worker.worker_id))) is None


async def test_a_background_loop_ends_when_a_round_turns_a_cancel_into_an_error() -> (
    None
):
    from apipi.common.background import run_loop

    entered = asyncio.Event()
    task = asyncio.create_task(
        run_loop("cancel_test", lambda: _closed_on_cancel(entered), interval=0)
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=5)
    assert task in done
    assert task.cancelled()
