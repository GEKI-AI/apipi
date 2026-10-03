import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from tests.api.test_workers import _leased_session, _worker_settings
from tests.support.fake_worker import FakeWorker
from tests.support.prom import metric_line
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.protocol import (
    BASELINE_FEATURES,
    FEATURE_SESSION_STOPPED,
    SUPPORTED_FEATURES,
    strict_parse,
)
from apipi.services import ingest as ingest_module
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.repo import get_session
from apipi.workerhub import serve as serve_module


def _app(settings: Settings, store: Store, **kw: Any):
    return create_app(
        api_settings_for(
            _worker_settings(settings, **kw).model_copy(update={"metrics": True})
        ),
        store=store,
    )


def _status(session_id: uuid.UUID, seq: int, **extra: Any) -> dict[str, Any]:
    return {
        "v": 2,
        "session_id": str(session_id),
        "seq": seq,
        "type": "session.status",
        "payload": {"status": "idle", **extra},
    }


def _presign(session_id: uuid.UUID, seq: int, request_id: uuid.UUID) -> dict[str, Any]:
    return {
        "v": 2,
        "session_id": str(session_id),
        "seq": seq,
        "type": "artifact.presign",
        "payload": {
            "request_id": str(request_id),
            "kind": "artifact",
            "filename": "out.txt",
            "content_type": "text/plain",
            "size": 5,
        },
    }


def _deadlock() -> OperationalError:
    return OperationalError("UPDATE", {}, Exception("deadlock detected"))


async def test_hello_and_register_carry_features(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = _app(settings, store)
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(features=["search"])
    assert set(hello["features"]) == SUPPORTED_FEATURES
    conn = app.state.workers.get(uuid.UUID(hello["worker_id"]))
    assert conn is not None and conn.features == frozenset({"search"})
    await worker.close()
    plain = FakeWorker(app, worker_secret, worker_id=hello["worker_id"])
    await plain.connect()
    conn = app.state.workers.get(uuid.UUID(hello["worker_id"]))
    assert conn is not None and conn.features == BASELINE_FEATURES
    await plain.close()


async def test_transient_ingest_error_is_retried_and_acked_after_the_retry(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_module, "RETRY_DELAYS", (0.01, 0.01, 0.01))
    app = _app(settings, store)
    real = ingest_module._apply
    state: dict[str, Any] = {"calls": 0, "ack_early": None}

    async def flaky(db, bus, envelope, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise _deadlock()
        state["ack_early"] = not worker.ws._outgoing.empty()
        return await real(db, bus, envelope, *args, **kwargs)

    monkeypatch.setattr(ingest_module, "_apply", flaky)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        await worker.send_json(_status(session_id, 1))
        ack = await worker.receive_json()
        assert ack == {"type": "ack", "session_id": str(session_id), "last_seq": 1}
        assert state["ack_early"] is False
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None and row.status == "idle" and row.worker_seq == 1
        body = app.state.metrics.scrape().decode()
        assert metric_line(
            body,
            "apipi_worker_ingest_total",
            type="session.status",
            result="transient_error",
        ).endswith(" 1.0")
        assert metric_line(
            body, "apipi_worker_protocol_total", event="ingest.retried"
        ).endswith(" 1.0")
        await worker.close()


async def test_ingest_closes_the_socket_when_a_transient_error_persists(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_module, "RETRY_DELAYS", (0.01, 0.01, 0.01))
    app = _app(settings, store)

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise _deadlock()

    monkeypatch.setattr(ingest_module, "_apply", broken)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        await worker.send_json(_status(session_id, 1))
        closed = await worker.wait_close()
        assert closed["code"] == 1011
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None and row.worker_seq == 0


async def test_presign_reply_comes_before_the_ack_and_a_replay_gets_it_again(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = _app(settings, store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        _tenant, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        request_id = uuid.uuid4()
        await worker.send_json(_presign(session_id, 1, request_id))
        first = await worker.receive_json()
        assert first["type"] == "artifact.presign.reply"
        assert (await worker.receive_json())["type"] == "ack"
        await worker.send_json(_presign(session_id, 1, request_id))
        again = await worker.receive_json()
        assert again["type"] == "artifact.presign.reply"
        assert again["upload_id"] == first["upload_id"]
        assert again["request_id"] == first["request_id"] == str(request_id)
        assert (await worker.receive_json())["type"] == "ack"
        await worker.close()


async def test_new_fields_and_types_from_a_newer_worker_lose_nothing(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = _app(settings, store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        with strict_parse(False):
            await worker.send_json(_status(session_id, 1, shiny="new"))
            await worker.send_json(
                {
                    "v": 2,
                    "session_id": str(session_id),
                    "seq": 2,
                    "type": "future.thing",
                    "payload": {},
                }
            )
            await worker.send_json({"type": "future.control"})
            await worker.send_json({"type": "heartbeat", "future": True})
            ack = await worker.receive_json()
            assert ack["last_seq"] == 1
            for _ in range(100):
                body = app.state.metrics.scrape().decode()
                if 'event="unknown_type"' in body and 'event="unknown_field"' in body:
                    break
                await asyncio.sleep(0.01)
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None and row.status == "idle" and row.worker_seq == 1
        assert metric_line(
            body, "apipi_worker_protocol_total", event="unknown_type"
        ).endswith(" 2.0")
        assert metric_line(
            body, "apipi_worker_protocol_total", event="unknown_field"
        ).endswith(" 2.0")
        await worker.close()


async def test_commands_of_a_lease_queue_in_order_and_are_acked_one_by_one(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = _app(settings, store)
    hub = app.state.workers
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, first = await _leased_session(app, client, store, worker)
        from apipi.protocol import TurnCancelCommandPayload

        second = await hub.command(
            store,
            tenant_id,
            session_id,
            op="turn.cancel",
            payload=TurnCancelCommandPayload(tenant_id=tenant_id),
        )
        assert second is not None and second["id"] != first["id"]
        await worker.receive_json()
        lease_id = uuid.UUID(first["lease_id"])
        assert [e.command_id for e in hub.commands.for_lease(lease_id)] == [
            first["id"],
            second["id"],
        ]
        await worker.close()
        again = FakeWorker(app, worker_secret, worker_id=worker.worker_id)
        await again.connect()
        assert (await again.receive_json())["id"] == first["id"]
        assert (await again.receive_json())["id"] == second["id"]
        await again.send_json(
            {"type": "lease.ack", "id": second["id"], "lease_id": first["lease_id"]}
        )
        for _ in range(100):
            if len(hub.commands.for_lease(lease_id)) == 1:
                break
            await asyncio.sleep(0.01)
        assert [e.command_id for e in hub.commands.for_lease(lease_id)] == [first["id"]]
        await again.close()


async def test_zombie_lease_is_reattached_and_its_command_is_sent_again(
    settings: Settings,
    store: Store,
    worker_secret: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    app = _app(settings, store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        dying = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, dying
        )
        worker_id = dying.worker_id
        await dying.close()
        other = {"session_id": str(uuid.uuid4()), "lease_id": str(uuid.uuid4())}
        back = FakeWorker(app, worker_secret, worker_id=worker_id)
        hello = await back.connect(running=[other])
        assert hello["sessions"] == {str(session_id): 0}
        resent = await back.receive_json()
        assert resent["id"] == command["id"]
        assert not [
            r
            for r in caplog.records
            if getattr(r, "event", "") == "worker.lease.orphaned"
        ]
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None and str(row.lease_id) == command["lease_id"]
        await back.send_json(
            {"type": "lease.ack", "id": command["id"], "lease_id": command["lease_id"]}
        )
        for _ in range(100):
            if not app.state.workers.commands.has_lease(uuid.UUID(command["lease_id"])):
                break
            await asyncio.sleep(0.01)
        assert not app.state.workers.commands.has_lease(uuid.UUID(command["lease_id"]))
        await back.close()


async def test_unacked_commands_are_sent_again_on_a_timer_then_age_out(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = _app(settings, store, worker_lease_ttl=timedelta(seconds=30))
    hub = app.state.workers
    hub.retransmit_seconds = 0.0
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, worker
        )
        await hub.retransmit_due(store, app.state.event_hub)
        resent = await worker.receive_json()
        assert resent["id"] == command["id"]
        body = app.state.metrics.scrape().decode()
        assert metric_line(
            body,
            "apipi_worker_commands_total",
            op="turn.cancel",
            result="retransmitted",
        ).endswith(" 1.0")
        lease_id = uuid.UUID(command["lease_id"])
        for entry in hub.commands.for_lease(lease_id):
            entry.created -= 60
        await hub.retransmit_due(store, app.state.event_hub)
        assert not hub.commands.has_lease(lease_id)
        revoke = await worker.receive_json()
        assert revoke["type"] == "lease.revoke"
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None and row.lease_id is None
            events = await list_events(db, tenant_id, session_id)
        assert "worker_command_timeout" in [
            e.data.get("code") for e in events if e.type == "agent.session.error"
        ]
        body = app.state.metrics.scrape().decode()
        assert metric_line(
            body, "apipi_worker_commands_total", op="turn.cancel", result="expired"
        ).endswith(" 1.0")
        await worker.close()


async def test_session_stop_is_acked_on_receipt_and_completed_by_the_envelope(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    import json

    from tests.api.test_split_lease_seq_release import (
        TOKEN,
        _lease_of,
        _message,
        _new_session,
    )
    from tests.support.split_worker import split_client_for, wait_for_idle

    sent: list[str] = []
    async with split_client_for(settings, store, token=worker_secret, sent=sent) as (
        app,
        client,
        worker,
    ):
        session_id = await _new_session(client)
        assert (await _message(client, session_id, "hi")).status_code == 200
        await wait_for_idle(client, TOKEN, str(session_id))
        assert FEATURE_SESSION_STOPPED in app.state.workers.features_of(
            next(iter(app.state.workers._conns))
        )
        await app.state.execution.teardown(session_id)
        assert await _lease_of(store, session_id) is None
        frames = [json.loads(text) for text in sent]
        acks = [i for i, f in enumerate(frames) if f.get("type") == "lease.ack"]
        stopped = [
            i for i, f in enumerate(frames) if f.get("type") == "session.stopped"
        ]
        assert acks and stopped
        assert acks[-1] < stopped[0]
        assert worker.outbox.acked_seq(session_id) == worker.outbox.high_water(
            session_id
        )


async def test_a_restarted_worker_can_still_replay_its_spool_into_the_old_lease(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = _app(settings, store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        before = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, before
        )
        await before.send_json(
            {"type": "lease.ack", "id": command["id"], "lease_id": command["lease_id"]}
        )
        await before.close()
        restarted = FakeWorker(app, worker_secret, worker_id=before.worker_id)
        hello = await restarted.connect(running=[])
        assert hello["sessions"] == {str(session_id): 0}
        await restarted.send_json(_status(session_id, 1))
        ack = await restarted.receive_json()
        assert ack["last_seq"] == 1
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            assert str(row.lease_id) == command["lease_id"]
            assert row.status == "idle" and row.worker_seq == 1
        await restarted.send_json({"type": "inventory", "sessions": []})
        for _ in range(100):
            async with store.session() as db:
                row = await get_session(db, tenant_id, session_id)
                assert row is not None
                if row.lease_id is None:
                    break
            await asyncio.sleep(0.01)
        assert row.lease_id is None
        await restarted.close()
