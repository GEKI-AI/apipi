"""Split-mode credential-less artifact uploads (#448).

The worker holds no object-store credentials and performs no
artifact/file database writes. All uploads go through
`artifact.presign` -> reply -> PUT (S3) or shared-root write
(filesystem) -> `artifact.completed`, for artifact, pi_session, and
input_image kinds, via the outbox. Combined mode keeps the direct
path. Quota errors surface with today's codes.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from apipi.config import Settings
from apipi.services.ingest import IngestBatcher, flush_batch
from apipi.store.blobs import NS_ARTIFACTS, S3Store
from apipi.store.engine import Store
from apipi.store.repo import (
    create_session,
    create_tenant,
    get_file,
    list_artifacts,
    set_session_lease,
)
from apipi.worker.artifact_upload import (
    handle_presign_reply,
    upload_via_presign,
)
from apipi.worker.outbox import Outbox


def _s3_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "api-sessions"),
        local_store_dir=str(tmp_path / "api-shared"),
        artifact_store="s3",
        s3_bucket="test-bucket",
        s3_prefix="apipi/artifacts",
    )


def _fs_settings(tmp_path: Path, name: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / f"{name}-sessions"),
        local_store_dir=str(tmp_path / "shared"),
    )


async def _make_session(store: Store) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    from datetime import timedelta

    from apipi.store.models import utc_now

    worker_id = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db,
            tenant.id,
            environment={"type": "openai_hosted", "directory": "/tmp/ws"},
            key_id="key-1",
        )
        tenant_id = tenant.id
        session_id = row.id
        key_id = row.key_id
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
    return tenant_id, session_id, worker_id, key_id


def _queued(envelopes: list[dict[str, Any]]) -> list[Any]:
    from apipi.worker.protocol import parse_envelope

    batcher = IngestBatcher()
    for envelope in envelopes:
        parsed = parse_envelope(envelope)
        import json

        raw = len(json.dumps(envelope, separators=(",", ":")).encode())
        batcher.add(parsed, raw)
    return batcher.take()


async def _flush_outbox(
    store: Store,
    outbox: Outbox,
    session_id: uuid.UUID,
    worker_id: uuid.UUID,
    settings: Settings,
    objects: Any | None = None,
) -> Any:
    pending = outbox.pending(session_id)
    assert pending, "outbox has nothing to flush"
    queued = _queued(pending)
    outcome = await flush_batch(
        store,
        queued,
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        objects=objects,
    )
    for sid, last_seq in outcome.acks.items():
        outbox.acked(sid, last_seq)
    return outcome


async def _upload_roundtrip(
    store: Store,
    outbox: Outbox,
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]],
    worker_settings: Settings,
    api_settings: Settings,
    worker_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    kind: str,
    filename: str,
    content_type: str,
    data: bytes,
    api_objects: Any | None = None,
) -> dict[str, Any]:
    """Drive one presign upload: worker outbox -> API -> reply -> worker."""
    task = asyncio.create_task(
        upload_via_presign(
            outbox,
            waiters,
            worker_settings,
            session_id,
            kind=kind,
            filename=filename,
            content_type=content_type,
            data=data,
        )
    )
    # Wait for the presign envelope, flush it, feed the reply back.
    reply: dict[str, Any] | None = None
    for _ in range(100):
        if outbox.pending(session_id):
            break
        await asyncio.sleep(0.01)
    outcome = await _flush_outbox(
        store, outbox, session_id, worker_id, api_settings, objects=api_objects
    )
    assert outcome.presign_replies, f"no presign reply: {outcome.rejected}"
    reply = outcome.presign_replies[0]
    assert reply["ok"] is True, reply
    handled = handle_presign_reply(waiters, reply)
    assert handled is True
    result = await asyncio.wait_for(task, timeout=10)
    # Flush the completed envelope the worker appended.
    if outbox.pending(session_id):
        outcome2 = await _flush_outbox(
            store, outbox, session_id, worker_id, api_settings, objects=api_objects
        )
        assert outcome2.rejected == [], outcome2.rejected
    return result


async def _s3_split_flow(store: Store, tmp_path: Path, monkeypatch: Any) -> None:
    from tests.unit.test_blobs import FakeS3

    api_settings = _s3_settings(tmp_path)
    worker_settings = _s3_settings(tmp_path)
    fake = FakeS3()
    api_objects = S3Store(api_settings, client=fake)

    async def _fake_put(
        url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> None:
        # The worker PUTs with no credentials; route the bytes into FakeS3.
        # FakeS3 presign URLs look like https://bucket.example/{key}?...
        # The key includes the s3 prefix; find it in the recorded presigns.
        assert fake.presigns, "no presigned URL was issued"
        last = fake.presigns[-1]
        assert isinstance(last.get("Key"), str)
        fake.objects[last["Key"]] = data

    monkeypatch.setattr("apipi.worker.artifact_upload.put_via_url", _fake_put)

    tenant_id, session_id, worker_id, _key_id = await _make_session(store)
    outbox = Outbox()
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}

    # The worker is credential-less: no blobs/objects on the execution.
    from apipi.services.runtime import FakeHarness
    from apipi.worker.execution import local_execution

    execution = local_execution(
        worker_settings,
        store=store,
        harness=FakeHarness(),
        outbox=outbox,
    )
    assert execution.blobs is None
    assert execution.objects is None

    # No artifact/file rows before the API ingests the worker's uploads.
    async with store.session() as db:
        assert await list_artifacts(db, tenant_id, session_id) == []
    data = b"artifact-bytes-s3"
    # Drive one artifact via the presign roundtrip helper (proves the
    # worker itself wrote nothing: rows appear only after API ingest).
    await _upload_roundtrip(
        store,
        outbox,
        waiters,
        worker_settings,
        api_settings,
        worker_id,
        session_id,
        kind="artifact",
        filename="outputs/out.bin",
        content_type="application/octet-stream",
        data=data,
        api_objects=api_objects,
    )
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None and len(artifacts) == 1
        assert artifacts[0].path == "outputs/out.bin"
        # The object lives under the session prefix; the worker never
        # saw credentials (the mocked PUT used only the URL).
        from apipi.store.blobs import blob_key

        async with store.session() as db2:
            from apipi.store.repo import get_session

            row = await get_session(db2, tenant_id, session_id)
            assert row is not None
            object_id = blob_key(tenant_id, row.key_id, session_id, artifacts[0].id)
        stored = await api_objects.get(NS_ARTIFACTS, object_id)
        assert stored == data

    # Pi session save through the same flow; the API updates the
    # session pointer the #447 turn context uses for cold restore.
    pi_data = b'{"history": ["hi"]}'
    await _upload_roundtrip(
        store,
        outbox,
        waiters,
        worker_settings,
        api_settings,
        worker_id,
        session_id,
        kind="pi_session",
        filename="pi-session.jsonl",
        content_type="application/octet-stream",
        data=pi_data,
        api_objects=api_objects,
    )
    async with store.session() as db:
        from apipi.store.repo import get_session

        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.pi_session_id is not None
    # Cold restore via the context GET ref (presigned URL for S3).
    from apipi.services.turn_context import (
        build_turn_context,
        fetch_pi_session_bytes,
    )

    context = await build_turn_context(
        store, api_settings, tenant_id, session_id, objects=api_objects
    )
    assert context["pi_session"]["present"] is True
    assert context["pi_session"]["url"]

    async def _fake_get(url: str) -> bytes:
        parsed = urlparse(url)
        key = parsed.path.lstrip("/")
        data = fake.objects.get(key)
        assert data is not None, f"missing fake S3 key {key}"
        return data

    import httpx

    real_client = httpx.AsyncClient

    class _FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content
            self.status_code = 200

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
            return _FakeResponse(await _fake_get(url))

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    try:
        restored = await fetch_pi_session_bytes(context["pi_session"], worker_settings)
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", real_client)
    assert restored == pi_data

    # Input image through the same flow; the API creates the file row.
    image = b"\x89PNG-input"
    result = await _upload_roundtrip(
        store,
        outbox,
        waiters,
        worker_settings,
        api_settings,
        worker_id,
        session_id,
        kind="input_image",
        filename="image",
        content_type="image/png",
        data=image,
        api_objects=api_objects,
    )
    assert result.get("file_id")
    async with store.session() as db:
        file_row = await get_file(db, tenant_id, result["file_id"])
        assert file_row is not None
        assert file_row.size == len(image)
    # The bytes live in FakeS3 under the s3 key; the file row proves
    # the API recorded them (HEAD verified the PUT before completing).


async def _fs_split_flow(store: Store, tmp_path: Path) -> None:
    api_settings = _fs_settings(tmp_path, "api")
    worker_settings = _fs_settings(tmp_path, "worker")
    # Separate sessions dirs, one shared store root.
    assert api_settings.sessions_dir != worker_settings.sessions_dir
    from apipi.worker.pi.dirs import store_root

    assert store_root(api_settings) == store_root(worker_settings)

    tenant_id, session_id, worker_id, _key_id = await _make_session(store)
    outbox = Outbox()
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}

    from apipi.services.runtime import FakeHarness
    from apipi.worker.execution import local_execution

    execution = local_execution(
        worker_settings, store=store, harness=FakeHarness(), outbox=outbox
    )
    assert execution.blobs is None
    assert execution.objects is None

    data = b"artifact-bytes-fs"
    await _upload_roundtrip(
        store,
        outbox,
        waiters,
        worker_settings,
        api_settings,
        worker_id,
        session_id,
        kind="artifact",
        filename="outputs/out.bin",
        content_type="application/octet-stream",
        data=data,
    )
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None and len(artifacts) == 1
        assert artifacts[0].path == "outputs/out.bin"
    # The bytes live under the shared root; the API reads them back.
    from apipi.store.blobs import LocalStore, blob_key
    from apipi.store.repo import get_session

    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        object_id = blob_key(tenant_id, row.key_id, session_id, artifacts[0].id)
    local = LocalStore(api_settings)
    assert await local.get(NS_ARTIFACTS, object_id) == data

    pi_data = b'{"history": ["fs"]}'
    await _upload_roundtrip(
        store,
        outbox,
        waiters,
        worker_settings,
        api_settings,
        worker_id,
        session_id,
        kind="pi_session",
        filename="pi-session.jsonl",
        content_type="application/octet-stream",
        data=pi_data,
    )
    from apipi.services.turn_context import (
        build_turn_context,
        fetch_pi_session_bytes,
    )

    context = await build_turn_context(store, api_settings, tenant_id, session_id)
    assert context["pi_session"]["present"] is True
    assert context["pi_session"]["local_path"]
    restored = await fetch_pi_session_bytes(context["pi_session"], worker_settings)
    assert restored == pi_data

    image = b"\x89PNG-fs"
    result = await _upload_roundtrip(
        store,
        outbox,
        waiters,
        worker_settings,
        api_settings,
        worker_id,
        session_id,
        kind="input_image",
        filename="image",
        content_type="image/png",
        data=image,
    )
    assert result.get("file_id")
    async with store.session() as db:
        file_row = await get_file(db, tenant_id, result["file_id"])
        assert file_row is not None


@pytest.mark.anyio
async def test_split_artifacts_s3_credentialless(
    store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    await _s3_split_flow(store, tmp_path, monkeypatch)


@pytest.mark.anyio
async def test_split_artifacts_filesystem_shared_root(
    store: Store, tmp_path: Path
) -> None:
    await _fs_split_flow(store, tmp_path)


@pytest.mark.anyio
async def test_split_artifacts_shared_root_mismatch(
    store: Store, tmp_path: Path
) -> None:
    """Separate store roots fail the shared-root check end to end."""
    from apipi.worker.hub import answer_store_check

    api_settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "api-sessions"),
        local_store_dir=str(tmp_path / "api-shared"),
    )
    worker_settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "worker-sessions"),
        local_store_dir=str(tmp_path / "worker-shared"),
    )
    from apipi.services.worker_artifacts import write_store_check
    from apipi.worker.pi.dirs import store_root

    marker, nonce = write_store_check(store_root(api_settings))
    hello = {"store_check": {"marker": marker, "nonce": nonce}}
    # The worker sees a different root, so it cannot prove it: the
    # register proof rejects it with the documented shared-path error.
    with pytest.raises(Exception, match="shared path"):
        answer_store_check(worker_settings, hello)
    from apipi.services.worker_artifacts import read_store_check

    assert read_store_check(store_root(api_settings), marker, nonce) is True
    assert read_store_check(store_root(worker_settings), marker, nonce) is False
    _ = hashlib.sha256(b"x").hexdigest()
