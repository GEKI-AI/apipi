"""Lifecycle, sandbox status, reaper wipes as durable v2 events (#449)."""

import uuid
from datetime import timedelta
from typing import Any

from apipi.protocol import WorkerEnvelope
from apipi.services.ingest import IngestBatcher, flush_batch
from apipi.services.lifecycle_export import LifecycleEmitter, api_heartbeat_loop
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    clear_session_lease,
    create_session,
    create_tenant,
    get_session_by_id,
    set_session_lease,
)


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


async def test_only_stopped_deletes_blobs(store: Store, settings) -> None:
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
    # Only the commanded stop deletes the artifact store: the idle
    # reap is a receipt only, so a session leased again before its
    # receipt lands keeps its artifacts.
    assert outcome.wipes == [(tenant_id, "", session_id)]


async def test_stopped_after_release_is_acked_past(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _hosted(store, worker_id)
    async with store.session() as db:
        await clear_session_lease(db, tenant_id, session_id)
    outcome = await _flush(
        store,
        worker_id,
        [_envelope(session_id, 1, "session.stopped", {"reason": "stop"})],
        settings,
    )
    # The normal stop already deleted the blobs synchronously; the
    # late receipt is rejected but acked past so the worker drops it.
    assert [reason for _, _, reason in outcome.rejected] == ["not_leased"]
    assert outcome.acks == {session_id: 1}
    assert outcome.wipes == []


async def test_reaped_unknown_session_is_acked_past(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    session_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [_envelope(session_id, 1, "workspace.reaped", {"reason": "idle"})],
        settings,
    )
    # The worker already wiped its local directory; there is no row
    # and no blob delete, and the receipt is acked past.
    assert [reason for _, _, reason in outcome.rejected] == ["not_leased"]
    assert outcome.acks == {session_id: 1}
    assert outcome.wipes == []


async def test_lifecycle_events_export_once_on_replay(store: Store, settings) -> None:
    from apipi.services.ingest import emit_lifecycle_intents

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
    first = await _flush(store, worker_id, flow, settings)
    assert first.rejected == []
    # Nothing is exported inside the transaction: the intents wait for
    # commit so a rolled back batch cannot export twice on replay.
    assert len(emitter.starts) == 0
    assert len(emitter.stops) == 0
    assert [intent.kind for intent in first.lifecycle] == ["start", "stop"]
    emit_lifecycle_intents(emitter, first.lifecycle)
    assert len(emitter.starts) == 1
    assert emitter.starts[0][1] == "spawn"
    assert emitter.starts[0][0]["session_id"] == str(session_id)
    assert len(emitter.stops) == 1
    assert emitter.stops[0][1] == "stop"
    assert emitter.stops[0][2] == 9
    replayed = await _flush(store, worker_id, flow, settings)
    assert replayed.rejected == []
    assert replayed.acks == {session_id: 2}
    assert replayed.lifecycle == []
    emit_lifecycle_intents(emitter, replayed.lifecycle)
    assert len(emitter.starts) == 1
    assert len(emitter.stops) == 1


async def test_lifecycle_identity_comes_from_row(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _hosted(store, worker_id)
    spoofed = str(uuid.uuid4())
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "lifecycle.start",
                {
                    "cause": "spawn",
                    "tenant_id": spoofed,
                    "org_id": spoofed,
                    "agent_id": spoofed,
                    "user_id": spoofed,
                    "key_id": spoofed,
                    "sandbox_image": "worker-img",
                    "run_mode": "microvm",
                },
            ),
        ],
        settings,
    )
    assert outcome.rejected == []
    assert len(outcome.lifecycle) == 1
    fields = outcome.lifecycle[0].fields
    assert fields["session_id"] == str(session_id)
    assert fields["tenant_id"] == str(tenant_id)
    assert fields["org_id"] is None
    assert fields["agent_id"] is None
    assert fields["user_id"] is None
    assert fields["key_id"] is None
    assert fields["sandbox_image"] == "worker-img"
    assert fields["run_mode"] == "microvm"


async def test_reporter_start_validates_strictly() -> None:
    from apipi.protocol import LifecycleStartPayload
    from apipi.services.lifecycle_export import OutboxLifecycleReporter
    from apipi.worker.outbox import Outbox

    outbox = Outbox()
    reporter = OutboxLifecycleReporter(outbox)
    session_id = uuid.uuid4()
    seq = reporter.emit_start(
        {
            "session_id": session_id,
            "tenant_id": str(uuid.uuid4()),
            "born": 1.0,
            "environment_type": "openai_hosted",
            "sandbox_image": "img",
            "run_mode": "microvm",
            "started_at": "2026-01-01T00:00:00Z",
        },
        cause="spawn",
    )
    assert seq == 1
    pending = outbox.pending(session_id)
    assert len(pending) == 1
    parsed = LifecycleStartPayload.model_validate(pending[0]["payload"])
    assert parsed.cause == "spawn"
    assert parsed.sandbox_image == "img"


class _Conn:
    def __init__(self, run_mode: str) -> None:
        self.run_mode = run_mode


class _Hub:
    def __init__(
        self, session_id: uuid.UUID, connections: dict[uuid.UUID, _Conn]
    ) -> None:
        self.session_id = session_id
        self.connections = connections

    def known_live_sessions(self) -> list[uuid.UUID]:
        return [self.session_id]

    def get(self, worker_id: uuid.UUID) -> _Conn | None:
        return self.connections.get(worker_id)


class _Heartbeat:
    heartbeat_s: float | None = 60.0

    def __init__(self) -> None:
        self.entries: list[list[dict[str, Any]]] = []

    def emit_heartbeat(self, entries: list[dict[str, Any]]) -> int:
        self.entries.append(entries)
        return 1


async def _heartbeat_entries(
    store: Store, settings, connections: dict[uuid.UUID, _Conn], worker_id: uuid.UUID
) -> tuple[uuid.UUID, list[dict[str, Any]]]:
    _tenant, session_id, _lease = await _hosted(store, worker_id)
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
        _Hub(session_id, connections),
        store,
        sleep=sleep,
        clock=now,
        max_emits=1,  # type: ignore[arg-type]
    )
    assert len(emitter.entries) == 1
    return session_id, emitter.entries[0]


async def test_api_heartbeat_derives_from_inventory(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    session_id, entries = await _heartbeat_entries(
        store, settings, {worker_id: _Conn("microvm")}, worker_id
    )
    assert entries[0]["session_id"] == str(session_id)
    assert entries[0]["environment_type"] == "openai_hosted"
    assert entries[0]["run_mode"] == "microvm"


async def test_api_heartbeat_entry_without_connection_has_no_run_mode(
    store: Store, settings
) -> None:
    session_id, entries = await _heartbeat_entries(store, settings, {}, uuid.uuid4())
    assert entries[0]["session_id"] == str(session_id)
    assert entries[0]["run_mode"] is None


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
