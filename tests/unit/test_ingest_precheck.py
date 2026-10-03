import hashlib
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

import pytest

from apipi.common.dirs import store_root
from apipi.config import Settings
from apipi.protocol import parse_envelope
from apipi.services import worker_artifacts
from apipi.services.ingest import QueuedEnvelope, flush_batch
from apipi.store.blobs import MemoryStore
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_artifact,
    create_session,
    create_tenant,
    set_session_lease,
)


class CountingStore:
    """A store proxy that counts the database sessions open right now."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.open = 0

    @asynccontextmanager
    async def session(self) -> Any:
        self.open += 1
        try:
            async with self._store.session() as db:
                yield db
        finally:
            self.open -= 1


class WatchedObjects(MemoryStore):
    def __init__(self, counting: CountingStore) -> None:
        super().__init__()
        self.counting = counting
        self.open_during_io: list[int] = []

    async def used_bytes(self, namespace: Any, prefix: str) -> int:
        self.open_during_io.append(self.counting.open)
        return await super().used_bytes(namespace, prefix)

    async def digest(self, namespace: Any, object_id: str) -> Any:
        self.open_during_io.append(self.counting.open)
        return await super().digest(namespace, object_id)


async def _leased(store: Store, worker_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(db, tenant.id, environment={"type": "none"})
        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=60),
        )
        return tenant.id, row.id


def _queued(session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]):
    envelope = parse_envelope(
        {
            "v": 2,
            "session_id": str(session_id),
            "seq": seq,
            "type": type,
            "payload": payload,
        }
    )
    return QueuedEnvelope(envelope, 200)


async def test_presign_store_reads_run_outside_any_database_session(
    store: Store, settings: Settings
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id = await _leased(store, worker_id)
    data = b"hello"
    async with store.session() as db:
        artifact = await create_artifact(
            db,
            tenant_id,
            session_id,
            path="out.txt",
            content_type="text/plain",
            byte_size=len(data),
            key_id="",
        )
        artifact_id = artifact.id
    counting = CountingStore(store)
    objects = WatchedObjects(counting)
    from apipi.common.objects import NS_ARTIFACTS
    from apipi.store.blobs import blob_key

    await objects.put(
        NS_ARTIFACTS, blob_key(tenant_id, "", session_id, artifact_id), data
    )
    request = uuid.uuid4()
    outcome = await flush_batch(
        cast(Any, counting),
        [
            _queued(
                session_id,
                1,
                "artifact.presign",
                {
                    "request_id": str(request),
                    "kind": "artifact",
                    "filename": "out.txt",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
        ],
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        objects=objects,
    )
    assert objects.open_during_io
    assert set(objects.open_during_io) == {0}
    assert outcome.presign_replies[0]["unchanged"] is True
    assert outcome.acks == {session_id: 1}


async def test_completed_filesystem_check_runs_in_a_thread_outside_the_row_lock(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id = await _leased(store, worker_id)
    counting = CountingStore(store)
    objects = WatchedObjects(counting)
    data = b"x" * 5000
    digest = hashlib.sha256(data).hexdigest()
    first = await flush_batch(
        cast(Any, counting),
        [
            _queued(
                session_id,
                1,
                "artifact.presign",
                {
                    "request_id": str(uuid.uuid4()),
                    "kind": "artifact",
                    "filename": "big.bin",
                    "size": len(data),
                    "sha256": digest,
                },
            )
        ],
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        objects=objects,
    )
    reply = first.presign_replies[0]
    assert reply["ok"] is True
    target = store_root(settings) / reply["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    seen: list[tuple[bool, int]] = []
    real = worker_artifacts._check_local_object

    def watched(*args: Any, **kwargs: Any) -> Any:
        seen.append(
            (threading.current_thread() is not threading.main_thread(), counting.open)
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(worker_artifacts, "_check_local_object", watched)
    second = await flush_batch(
        cast(Any, counting),
        [
            _queued(
                session_id,
                2,
                "artifact.completed",
                {
                    "upload_id": reply["upload_id"],
                    "path": reply["path"],
                    "size": len(data),
                    "sha256": digest,
                },
            )
        ],
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        objects=objects,
    )
    assert seen == [(True, 0)]
    assert second.rejected == []
    async with store.session() as db:
        from apipi.store.repo import list_artifacts

        stored = await list_artifacts(db, tenant_id, session_id)
    assert stored is not None
    assert [item.path for item in stored] == ["big.bin"]
    assert stored[0].byte_size == len(data)


async def test_a_store_failure_is_answered_like_before(
    store: Store, settings: Settings
) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id = await _leased(store, worker_id)
    counting = CountingStore(store)

    class Broken(WatchedObjects):
        async def used_bytes(self, namespace: Any, prefix: str) -> int:
            raise OSError("store down")

    outcome = await flush_batch(
        cast(Any, counting),
        [
            _queued(
                session_id,
                1,
                "artifact.presign",
                {
                    "request_id": str(uuid.uuid4()),
                    "kind": "artifact",
                    "filename": "a.txt",
                    "size": 3,
                },
            )
        ],
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        objects=Broken(counting),
    )
    assert outcome.presign_replies[0]["ok"] is False
    assert outcome.presign_replies[0]["code"] == "ingest_error"
