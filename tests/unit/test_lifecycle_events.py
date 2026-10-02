"""Lifecycle, sandbox status, reaper wipes as durable v2 events (#449)."""

import uuid
from datetime import timedelta
from typing import Any

from apipi.services.ingest import IngestBatcher, flush_batch
from apipi.services.lifecycle_export import LifecycleEmitter, api_heartbeat_loop
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    get_session_by_id,
    set_session_lease,
)
from apipi.worker.protocol import WorkerEnvelope


async def _hosted(
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
        return tenant.id, row.id, lease_id


def _envelope(
    session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]
) -> WorkerEnvelope:
    return WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "turn_id": None,
            "seq": seq,
            "type": type,
            "payload": payload,
        }
    )


async def _flush(
    store: Store,
    worker_id: uuid.UUID,
    envelopes: list[WorkerEnvelope],
    settings: Any = None,
    lifecycle: Any | None = None,
):
    batcher = IngestBatcher()
    for envelope in envelopes:
        batcher.add(envelope, 128)
    return await flush_batch(
        store,
        batcher.take(),
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        lifecycle=lifecycle,
    )


class _Emitter:
    def __init__(self) -> None:
        self.starts: list[tuple[dict[str, Any], str]] = []
        self.stops: list[tuple[dict[str, Any], str, int]] = []

    def emit_start(self, fields: dict[str, Any], *, cause: str) -> int:
        self.starts.append((fields, cause))
        return len(self.starts)

    def emit_stop(self, fields: dict[str, Any], *, reason: str, live_ms: int) -> int:
        self.stops.append((fields, reason, live_ms))
        return len(self.stops)


async def test_sandbox_status_ready_looks_like_today(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _hosted(store, worker_id)
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "sandbox.status",
                {
                    "status": "starting",
                    "image": "img",
                    "size": "S",
                    "cause": "spawn",
                },
            ),
            _envelope(
                session_id,
                2,
                "sandbox.status",
                {
                    "status": "ready",
                    "image": "img",
                    "image_version": "v1",
                    "size": "S",
                    "run_mode": "none",
                    "boot_ms": 12,
                    "lock_wait_ms": 1,
                    "setup_ms": 2,
                },
            ),
        ],
        settings,
    )
    assert outcome.rejected == []
    assert outcome.acks == {session_id: 2}
    kinds = [body["type"] for _, body in outcome.wakes]
    assert kinds == [
        "agent.session.environment.pending",
        "agent.session.environment.connected",
    ]
    connected = outcome.wakes[1][1]
    assert connected["data"]["sandbox"]["state"] == "ready"
    assert connected["data"]["sandbox"]["boot_ms"] == 12
    async with store.session() as db:
        row = await get_session_by_id(db, session_id)
        assert row is not None
        assert row.sandbox_state == "ready"
        assert row.sandbox_cold_boots == 1
        events = await list_events(db, row.tenant_id, session_id)
    assert [event.type for event in events] == [
        "agent.session.environment.pending",
        "agent.session.environment.connected",
    ]


async def test_sandbox_status_unknown_phase_rejected(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _hosted(store, worker_id)
    outcome = await _flush(
        store,
        worker_id,
        [_envelope(session_id, 1, "sandbox.status", {"status": "flying"})],
        settings,
    )
    assert [reason for _, _, reason in outcome.rejected] == ["invalid_envelope"]
    assert outcome.acks == {session_id: 1}


async def test_stopped_and_reaped_record_wipes(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _hosted(store, worker_id)
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(session_id, 1, "session.stopped", {"reason": "stop"}),
            _envelope(session_id, 2, "workspace.reaped", {"reason": "idle"}),
        ],
        settings,
    )
    assert outcome.rejected == []
    assert outcome.acks == {session_id: 2}
    assert [wipe[2] for wipe in outcome.wipes] == [session_id, session_id]
    assert all(wipe[0] == tenant_id for wipe in outcome.wipes)


async def test_lifecycle_events_export_once_on_replay(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _hosted(store, worker_id)
    emitter = _Emitter()
    flow = [
        _envelope(
            session_id,
            1,
            "lifecycle.start",
            {"cause": "spawn", "environment_type": "openai_hosted"},
        ),
        _envelope(
            session_id,
            2,
            "lifecycle.stop",
            {"reason": "stop", "live_ms": 9},
        ),
    ]
    first = await _flush(store, worker_id, flow, settings, lifecycle=emitter)
    assert first.rejected == []
    assert len(emitter.starts) == 1
    assert emitter.starts[0][1] == "spawn"
    assert emitter.starts[0][0]["session_id"] == str(session_id)
    assert len(emitter.stops) == 1
    assert emitter.stops[0][1] == "stop"
    assert emitter.stops[0][2] == 9
    replayed = await _flush(store, worker_id, flow, settings, lifecycle=emitter)
    assert replayed.rejected == []
    assert replayed.acks == {session_id: 2}
    assert len(emitter.starts) == 1
    assert len(emitter.stops) == 1


async def test_api_heartbeat_derives_from_inventory(store: Store, settings) -> None:
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
        session_id = row.id

    class _Hub:
        def known_live_sessions(self):
            return [session_id]

    class _Heartbeat:
        heartbeat_s: float | None = 60.0

        def __init__(self) -> None:
            self.entries: list[list[dict[str, Any]]] = []

        def emit_heartbeat(self, entries):
            self.entries.append(entries)
            return 1

    emitter = _Heartbeat()
    sleeps: list[float] = []

    async def sleep(wait: float) -> None:
        sleeps.append(wait)

    clock = [0.0]

    def now() -> float:
        clock[0] += 61.0
        return clock[0]

    await api_heartbeat_loop(
        settings,
        emitter,
        _Hub(),
        store,
        sleep=sleep,
        clock=now,
        max_emits=1,  # type: ignore[arg-type]
    )
    assert len(emitter.entries) == 1
    assert emitter.entries[0][0]["session_id"] == str(session_id)
    assert emitter.entries[0][0]["environment_type"] == "openai_hosted"


async def test_lifecycle_start_payload_fits_export_identity() -> None:
    from apipi.config import Settings

    emitter = LifecycleEmitter(Settings(lifecycle_export_url="http://x.test/"))
    seq = emitter.emit_start(
        {
            "cause": "spawn",
            "session_id": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "environment_type": "openai_hosted",
        },
        cause="spawn",
    )
    assert seq == 1
    pending = emitter.pending()
    assert pending[0]["type"] == "session.live.start"
    assert pending[0]["environment_type"] == "openai_hosted"
