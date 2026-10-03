import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from tests.support.prom import metric_line

from apipi.common.errors import ObjectStoreError
from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.protocol import WorkerEnvelope
from apipi.services import ingest as ingest_module
from apipi.services import worker_artifacts
from apipi.services.ingest import (
    UNKNOWN_TYPE,
    IngestBatcher,
    IngestOutcome,
    _Reject,
    classify_incoming,
    flush_batch,
)
from apipi.services.transient import is_transient
from apipi.services.worker_artifacts import sha256_hex
from apipi.store.blobs import blob_key
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    get_artifact_upload,
    get_session,
    list_artifacts,
    set_session_lease,
)
from apipi.worker.artifact_upload import write_shared_object


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _deadlock() -> OperationalError:
    return OperationalError("UPDATE sessions", {}, _Orig("40P01"))


async def _leased(store: Store, worker_id: uuid.UUID):
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}
        )
        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
        return tenant.id, row.id, row.key_id


def _envelope(session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]):
    return WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "seq": seq,
            "type": type,
            "payload": payload,
        }
    )


async def _flush(
    store: Store,
    worker_id: uuid.UUID,
    envelopes: list[WorkerEnvelope],
    settings: Any,
    metrics: Metrics | None = None,
) -> IngestOutcome:
    batcher = IngestBatcher()
    for envelope in envelopes:
        batcher.add(envelope, 128)
    return await flush_batch(
        store,
        batcher.take(),
        worker_id=worker_id,
        settings=settings,
        metrics=metrics,
    )


def _idle(session_id: uuid.UUID, seq: int) -> WorkerEnvelope:
    return _envelope(session_id, seq, "event", {"type": "agent.session.idle"})


async def test_deadlock_stops_the_ack_and_the_retry_applies_everything(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = Metrics()
    worker_id = uuid.uuid4()
    tenant_id, session_id, _key = await _leased(store, worker_id)
    real_apply = ingest_module._apply
    failures = {"left": 1}

    async def flaky(db, bus, envelope, *args, **kwargs):
        if envelope.seq == 2 and failures["left"]:
            failures["left"] -= 1
            raise _deadlock()
        return await real_apply(db, bus, envelope, *args, **kwargs)

    monkeypatch.setattr(ingest_module, "_apply", flaky)
    batch = [_idle(session_id, 1), _idle(session_id, 2), _idle(session_id, 3)]
    first = await _flush(store, worker_id, batch, settings, metrics)
    assert first.acks == {session_id: 1}
    assert first.rejected == []
    assert [item.envelope.seq for item in first.retry] == [2, 3]
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.worker_seq == 1
        assert len(await list_events(db, tenant_id, session_id)) == 1
    second = await flush_batch(
        store,
        first.retry,
        worker_id=worker_id,
        settings=settings,
        metrics=metrics,
    )
    assert second.acks == {session_id: 3}
    assert second.retry == []
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.worker_seq == 3
        assert len(await list_events(db, tenant_id, session_id)) == 3
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_ingest_total", type="event", result="transient_error"
    ).endswith(" 1.0")


async def test_transient_failure_of_one_session_does_not_hold_back_another(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_id = uuid.uuid4()
    _tenant_a, session_a, _ = await _leased(store, worker_id)
    _tenant_b, session_b, _ = await _leased(store, worker_id)
    real_apply = ingest_module._apply

    async def flaky(db, bus, envelope, *args, **kwargs):
        if envelope.session_id == session_a:
            raise _deadlock()
        return await real_apply(db, bus, envelope, *args, **kwargs)

    monkeypatch.setattr(ingest_module, "_apply", flaky)
    outcome = await _flush(
        store,
        worker_id,
        [_idle(session_a, 1), _idle(session_b, 1)],
        settings,
    )
    assert outcome.acks == {session_b: 1}
    assert [item.envelope.session_id for item in outcome.retry] == [session_a]


async def test_a_permanent_failure_is_still_acked_past(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _key = await _leased(store, worker_id)

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(ingest_module, "_apply", broken)
    outcome = await _flush(store, worker_id, [_idle(session_id, 1)], settings)
    assert outcome.acks == {session_id: 1}
    assert outcome.retry == []
    assert [reason for _, _, reason in outcome.rejected] == ["ingest_error"]


async def test_store_timeout_on_artifact_completed_is_not_acked_past(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, key_id = await _leased(store, worker_id)
    shared = tmp_path / "shared"
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        local_store_dir=str(shared),
    )
    data = b"artifact-bytes"
    presign = _envelope(
        session_id,
        1,
        "artifact.presign",
        {
            "request_id": str(uuid.uuid4()),
            "kind": "artifact",
            "filename": "out.txt",
            "content_type": "text/plain",
            "size": len(data),
            "sha256": sha256_hex(data),
        },
    )
    first = await _flush(store, worker_id, [presign], settings)
    upload_id = uuid.UUID(first.presign_replies[0]["upload_id"])
    async with store.session() as db:
        upload = await get_artifact_upload(db, tenant_id, upload_id)
        assert upload is not None
        object_id = blob_key(tenant_id, key_id, session_id, upload.artifact_id)
    relative = write_shared_object(settings, object_id, data)
    completed = _envelope(
        session_id,
        2,
        "artifact.completed",
        {
            "upload_id": str(upload_id),
            "path": relative,
            "size": len(data),
            "sha256": sha256_hex(data),
            "name": "out.txt",
        },
    )
    real_verify = worker_artifacts.verify_upload_object
    calls = {"count": 0}

    async def flaky(*args: Any, **kwargs: Any) -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ObjectStoreError(
                "Artifact store unavailable",
                operation="head",
                bucket="b",
                key="k",
                code="RequestTimeout",
            )
        return await real_verify(*args, **kwargs)

    monkeypatch.setattr(worker_artifacts, "verify_upload_object", flaky)
    outcome = await _flush(store, worker_id, [completed], settings)
    assert outcome.acks == {}
    assert outcome.rejected == []
    assert [item.envelope.seq for item in outcome.retry] == [2]
    async with store.session() as db:
        assert await list_artifacts(db, tenant_id, session_id) == []
    again = await flush_batch(
        store,
        outcome.retry,
        worker_id=worker_id,
        settings=settings,
        metrics=None,
    )
    assert again.acks == {session_id: 2}
    assert again.rejected == []
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None and len(artifacts) == 1


async def test_duplicate_presign_gets_the_same_reply_again(
    store: Store, tmp_path: Path
) -> None:
    metrics = Metrics()
    worker_id = uuid.uuid4()
    _tenant, session_id, _key = await _leased(store, worker_id)
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        local_store_dir=str(tmp_path / "shared"),
    )
    request_id = uuid.uuid4()
    presign = _envelope(
        session_id,
        1,
        "artifact.presign",
        {
            "request_id": str(request_id),
            "kind": "artifact",
            "filename": "out.txt",
            "content_type": "text/plain",
            "size": 5,
            "sha256": sha256_hex(b"hello"),
        },
    )
    first = await _flush(store, worker_id, [presign], settings, metrics)
    second = await _flush(store, worker_id, [presign], settings, metrics)
    assert second.acks == {session_id: 1}
    assert len(second.presign_replies) == 1
    reply = second.presign_replies[0]
    original = first.presign_replies[0]
    for name in ("request_id", "ok", "upload_id", "artifact_id", "path", "object_id"):
        assert reply[name] == original[name]
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_worker_protocol_total", event="presign.rereplied")


def test_unknown_envelope_type_is_not_garbage() -> None:
    with pytest.raises(_Reject) as caught:
        classify_incoming(
            {"v": 2, "session_id": str(uuid.uuid4()), "seq": 1, "type": "new.thing"}
        )
    assert caught.value.reason == UNKNOWN_TYPE


def test_oversize_is_measured_in_wire_bytes() -> None:
    from apipi.protocol import MAX_MESSAGE_BYTES

    text = "é" * (MAX_MESSAGE_BYTES // 2 - 200)
    data = {
        "v": 2,
        "session_id": str(uuid.uuid4()),
        "seq": 1,
        "type": "event",
        "payload": {"type": "agent.session.idle", "data": {"text": text}},
    }
    _kind, _parsed, size = classify_incoming(data)
    assert size < MAX_MESSAGE_BYTES
    assert size > len(text)


def test_transient_classification() -> None:
    assert is_transient(_deadlock())
    assert is_transient(TimeoutError())
    assert not is_transient(RuntimeError("bug"))
    assert not is_transient(ValueError("bad"))
    timeout = ObjectStoreError(
        "x", operation="head", bucket="", key="", code="SlowDown"
    )
    assert is_transient(timeout)
    missing = ObjectStoreError(
        "x", operation="head", bucket="", key="", code="NoSuchKey"
    )
    assert not is_transient(missing)
    assert not is_transient(
        worker_artifacts._store_error("upload size mismatch", operation="complete")
    )
