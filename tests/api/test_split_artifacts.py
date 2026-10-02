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


async def _make_session(
    store: Store, directory: str = "/tmp/ws"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    from datetime import timedelta

    from apipi.store.models import utc_now

    worker_id = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db,
            tenant.id,
            environment={"type": "openai_hosted", "directory": directory},
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


async def _pump_until_done(
    store: Store,
    outbox: Any,
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]],
    worker_id: uuid.UUID,
    session_id: uuid.UUID,
    api_settings: Settings,
    task: asyncio.Task[None],
    api_objects: Any,
) -> None:
    """Flush worker outbox envelopes through API ingest until `task` ends."""
    for _ in range(2000):
        if task.done():
            break
        if outbox.pending(session_id):
            outcome = await _flush_outbox(
                store, outbox, session_id, worker_id, api_settings, objects=api_objects
            )
            assert outcome.rejected == [], outcome.rejected
            for reply in outcome.presign_replies:
                assert reply["ok"] is True, reply
                assert handle_presign_reply(waiters, reply) is True
        await asyncio.sleep(0.005)
    await asyncio.wait_for(asyncio.shield(task), timeout=30)
    for _ in range(10):
        if not outbox.pending(session_id):
            break
        outcome = await _flush_outbox(
            store, outbox, session_id, worker_id, api_settings, objects=api_objects
        )
        assert outcome.rejected == [], outcome.rejected
        for reply in outcome.presign_replies:
            assert reply["ok"] is True, reply
            assert handle_presign_reply(waiters, reply) is True
    assert not outbox.pending(session_id)


async def _s3_testbed(store: Store, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """S3 split pair with a counting PUT mock; worker settings hold no keys."""
    from tests.unit.test_blobs import FakeS3

    api_settings = _s3_settings(tmp_path)
    worker_settings = _s3_settings(tmp_path)
    # S3 with no endpoint and no ambient credentials: any real S3
    # construction on the worker must fail loudly (patched below in
    # the turn test); here the worker only ever sees presigned URLs.
    assert api_settings.artifact_store == "s3"
    assert worker_settings.artifact_store == "s3"
    assert not (worker_settings.s3_endpoint or "").strip()
    fake = FakeS3()
    api_objects = S3Store(api_settings, client=fake)
    puts: list[tuple[str, bytes, dict[str, str]]] = []

    async def _fake_put(
        url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> None:
        assert fake.presigns, "no presigned URL was issued"
        last = fake.presigns[-1]
        assert isinstance(last.get("Key"), str)
        fake.objects[last["Key"]] = data
        puts.append((last["Key"], data, dict(headers or {})))

    monkeypatch.setattr("apipi.worker.artifact_upload.put_via_url", _fake_put)
    return {
        "api_settings": api_settings,
        "worker_settings": worker_settings,
        "fake": fake,
        "api_objects": api_objects,
        "puts": puts,
    }


@pytest.mark.anyio
async def test_split_artifact_presign_skips_unchanged(
    store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    """A second identical presign answers `unchanged`: no PUT, no new row."""
    bed = await _s3_testbed(store, tmp_path, monkeypatch)
    tenant_id, session_id, worker_id, _key_id = await _make_session(store)
    outbox = Outbox()
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}
    kwargs: dict[str, Any] = {
        "store": store,
        "outbox": outbox,
        "waiters": waiters,
        "worker_settings": bed["worker_settings"],
        "api_settings": bed["api_settings"],
        "worker_id": worker_id,
        "session_id": session_id,
        "kind": "artifact",
        "filename": "outputs/out.bin",
        "content_type": "application/octet-stream",
        "api_objects": bed["api_objects"],
    }
    first = await _upload_roundtrip(**kwargs, data=b"same-bytes")
    assert first.get("unchanged") is not True
    assert len(bed["puts"]) == 1
    second = await _upload_roundtrip(**kwargs, data=b"same-bytes")
    assert second.get("unchanged") is True
    assert len(bed["puts"]) == 1
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None and len(artifacts) == 1
    # Changed bytes still upload under the same path.
    third = await _upload_roundtrip(**kwargs, data=b"new-bytes")
    assert third.get("unchanged") is not True
    assert len(bed["puts"]) == 2
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None and len(artifacts) == 2


@pytest.mark.anyio
async def test_split_pi_session_reuses_blob_id(
    store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    """Pi saves overwrite one object; the pointer never moves to a new key."""
    bed = await _s3_testbed(store, tmp_path, monkeypatch)
    tenant_id, session_id, worker_id, _key_id = await _make_session(store)
    outbox = Outbox()
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}
    kwargs: dict[str, Any] = {
        "store": store,
        "outbox": outbox,
        "waiters": waiters,
        "worker_settings": bed["worker_settings"],
        "api_settings": bed["api_settings"],
        "worker_id": worker_id,
        "session_id": session_id,
        "kind": "pi_session",
        "filename": "pi-session.jsonl",
        "content_type": "application/octet-stream",
        "api_objects": bed["api_objects"],
    }
    await _upload_roundtrip(**kwargs, data=b'{"history": ["one"]}')
    from apipi.store.repo import get_session

    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.pi_session_id is not None
        first_pointer = row.pi_session_id
    first_keys = [key for key, _data, _headers in bed["puts"]]
    assert len(first_keys) == 1
    await _upload_roundtrip(**kwargs, data=b'{"history": ["one", "two"]}')
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None
        assert row.pi_session_id == first_pointer
    second_keys = [key for key, _data, _headers in bed["puts"]]
    assert len(second_keys) == 2
    assert second_keys[0] == second_keys[1]
    assert bed["fake"].objects[second_keys[1]] == b'{"history": ["one", "two"]}'


async def _pump_until_done(
    store: Store,
    outbox: Any,
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]],
    worker_id: uuid.UUID,
    session_id: uuid.UUID,
    api_settings: Settings,
    task: asyncio.Task[None],
    api_objects: Any,
) -> None:
    """Flush worker outbox envelopes through API ingest until `task` ends."""
    for _ in range(2000):
        if task.done():
            break
        if outbox.pending(session_id):
            outcome = await _flush_outbox(
                store, outbox, session_id, worker_id, api_settings, objects=api_objects
            )
            assert outcome.rejected == [], outcome.rejected
            for reply in outcome.presign_replies:
                assert reply["ok"] is True, reply
                assert handle_presign_reply(waiters, reply) is True
        await asyncio.sleep(0.005)
    await asyncio.wait_for(asyncio.shield(task), timeout=30)
    for _ in range(10):
        if not outbox.pending(session_id):
            break
        outcome = await _flush_outbox(
            store, outbox, session_id, worker_id, api_settings, objects=api_objects
        )
        assert outcome.rejected == [], outcome.rejected
        for reply in outcome.presign_replies:
            assert reply["ok"] is True, reply
            assert handle_presign_reply(waiters, reply) is True
    assert not outbox.pending(session_id)


@pytest.mark.anyio
async def test_split_turn_uploads_without_worker_store_or_row_writes(
    store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    """A real split turn through OutboxSink with a credential-less worker.

    Worker settings are S3 with no endpoint/credentials; constructing
    S3Store anywhere fails the test, as does any artifact/file row
    write or store-factory call from worker code. Rows appear only
    through API ingest, and a second identical turn uploads nothing.
    """
    import base64
    from datetime import timedelta

    from httpx import ASGITransport, AsyncClient

    from apipi.gateway import create_app
    from apipi.services.runtime import FakeHarness
    from apipi.store.blobs import S3Blobs
    from apipi.store.events import list_events
    from apipi.store.models import utc_now
    from apipi.worker.execution import local_execution
    from apipi.worker.hub import dispatch_command

    api_settings = _s3_settings(tmp_path)
    worker_settings = _s3_settings(tmp_path)
    worker_settings.sessions_dir = str(tmp_path / "worker-sessions")
    assert worker_settings.artifact_store == "s3"
    assert not (worker_settings.s3_endpoint or "").strip()

    from tests.unit.test_blobs import FakeS3

    fake = FakeS3()
    api_objects = S3Store(api_settings, client=fake)
    api_blobs = S3Blobs(api_settings, store=api_objects)
    app = create_app(
        api_settings,
        store=store,
        harness=FakeHarness(),
        objects=api_objects,
        blobs=api_blobs,
    )
    worker_id = uuid.uuid4()

    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    token = "split-turn"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test", "instructions": "Follow."},
        )
        assert agent.status_code == 200
        uploaded = await client.post(
            "/v1/files",
            headers=_auth(token),
            data={"purpose": "user_data"},
            files={"file": ("notes.txt", b"workspace notes", "text/plain")},
        )
        assert uploaded.status_code == 200
        file_id = str(uploaded.json()["id"])
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {
                    "type": "openai_hosted",
                    "files": [
                        {"type": "file_id", "file_id": file_id, "path": "notes.txt"}
                    ],
                },
            },
        )
        assert created.status_code == 200, created.json()
        from uuid import NAMESPACE_URL, uuid5

        from apipi.gateway.tokens import hash_token

        tenant_id = uuid5(NAMESPACE_URL, hash_token(token))
        session_id = uuid.UUID(created.json()["id"])

        from apipi.store.blobs import blob_key
        from apipi.store.repo import get_session, set_session_lease

        async with store.session() as db:
            directory_row = await get_session(db, tenant_id, session_id)
            assert directory_row is not None
            # The API assigns the hosted workspace directory on create.
            directory = directory_row.environment.get("directory")
            assert isinstance(directory, str) and directory
            workspace = Path(directory)

        (workspace / "outputs").mkdir(parents=True, exist_ok=True)
        (workspace / "outputs" / "result.txt").write_bytes(b"turn-output")

        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            pi_blob = uuid.uuid4()
            seed = b'{"history": ["seed"]}'
            await api_objects.put(
                NS_ARTIFACTS,
                blob_key(tenant_id, row.key_id, session_id, pi_blob),
                seed,
            )
            row.pi_session_id = pi_blob
            row.pi_session_bytes = len(seed)
            await set_session_lease(
                db,
                tenant_id,
                session_id,
                worker_id=worker_id,
                lease_id=uuid.uuid4(),
                lease_until=utc_now() + timedelta(seconds=120),
            )
            key_id = row.key_id

        transport = client._transport
        assert isinstance(transport, ASGITransport)
        from typing import cast

        real_app = cast(Any, transport.app)
        context = await real_app.state.sessions._turn_context(
            tenant_id,
            session_id,
            [],
            api_key="model-key",
            key_id=None,
            user_id=None,
            org_id=None,
        )
        assert context["pi_session"]["present"] is True
    # Worker-side guards: no store construction, no row writes from
    # worker code. API ingest still needs the real implementations, so
    # the factories and row writers only record their callers.
    import traceback

    import apipi.store.blobs as blobs_mod
    import apipi.store.repo as repo_mod

    def _boom_store(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("worker must not construct S3Store")

    monkeypatch.setattr(blobs_mod, "S3Store", _boom_store)

    # NOTE: no boom patches on runtime.get_session/FileService here:
    # API-side ingest runs in-process and legitimately uses them
    # (e.g. _write_turn_log). Turn-context independence is covered by
    # test_worker_runs_turn_from_context_without_db_reads; here the
    # recorded callers below prove worker code never builds stores or
    # writes rows.

    recorded: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    def _record(name: str, sync: bool, original: Any) -> Any:
        if sync:

            def _wrap(*args: Any, **kwargs: Any) -> Any:
                stack = tuple(
                    (frame.filename, frame.name) for frame in traceback.extract_stack()
                )
                recorded.append((name, stack))
                return original(*args, **kwargs)

            return _wrap

        async def _awrap(*args: Any, **kwargs: Any) -> Any:
            stack = tuple(
                (frame.filename, frame.name) for frame in traceback.extract_stack()
            )
            recorded.append((name, stack))
            return await original(*args, **kwargs)

        return _awrap

    monkeypatch.setattr(
        blobs_mod,
        "object_store",
        _record("object_store", True, blobs_mod.object_store),
    )
    monkeypatch.setattr(
        blobs_mod, "blob_store", _record("blob_store", True, blobs_mod.blob_store)
    )
    monkeypatch.setattr(
        repo_mod,
        "create_artifact",
        _record("create_artifact", False, repo_mod.create_artifact),
    )
    monkeypatch.setattr(
        repo_mod, "create_file", _record("create_file", False, repo_mod.create_file)
    )

    # The worker PUTs presigned bytes with no credentials; capture the
    # exact headers to prove no auth travels. GETs serve FakeS3.
    put_headers: list[dict[str, str]] = []
    put_keys: list[str] = []

    import httpx

    real_client = httpx.AsyncClient

    class _FakeResponse:
        def __init__(self, content: bytes = b"", status: int = 200) -> None:
            self.content = content
            self.status_code = status

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def put(
            self, url: str, content: bytes | None = None, headers: Any = None
        ) -> Any:
            assert fake.presigns, "no presigned URL was issued"
            last = fake.presigns[-1]
            assert isinstance(last.get("Key"), str)
            fake.objects[last["Key"]] = bytes(content or b"")
            put_keys.append(last["Key"])
            put_headers.append(dict(headers or {}))
            return _FakeResponse(b"", 200)

        async def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
            key = urlparse(url).path.lstrip("/")
            data = fake.objects.get(key)
            assert data is not None, f"missing fake S3 key {key}"
            return _FakeResponse(data, 200)

    outbox = Outbox()
    execution = local_execution(
        worker_settings, store=store, harness=FakeHarness(), outbox=outbox
    )
    assert execution.blobs is None
    assert execution.objects is None
    waiters = execution.presign_waiters

    def _turn_message(text: str, with_image: bool) -> dict[str, Any]:
        parts: list[dict[str, str]] = [{"type": "input_text", "text": text}]
        if with_image:
            image = base64.b64encode(b"\x89PNG-turn").decode()
            parts.append({"type": "image", "mimeType": "image/png", "data": image})
        return {
            "op": "turn.start",
            "session_id": str(session_id),
            "payload": {
                "tenant_id": str(tenant_id),
                "text": text,
                "parts": parts,
                "context": context,
            },
        }

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    try:
        task = asyncio.create_task(
            dispatch_command(execution, _turn_message("hi", True))
        )
        await _pump_until_done(
            store,
            outbox,
            waiters,
            worker_id,
            session_id,
            api_settings,
            task,
            api_objects,
        )
        async with store.session() as db:
            events = await list_events(db, tenant_id, session_id)
        assert any(event.type == "agent.session.turn.completed" for event in events), [
            event.type for event in events
        ]
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", real_client)

    # The workspace output and the input image landed through API ingest,
    # and the Pi save overwrote the seeded blob id instead of leaking.
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None
        paths = sorted(artifact.path for artifact in artifacts)
        assert paths == ["outputs/result.txt"], paths
        first_count = len(artifacts)
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.pi_session_id == pi_blob
        pi_object = blob_key(tenant_id, key_id, session_id, pi_blob)
        assert await api_objects.get(NS_ARTIFACTS, pi_object) == seed
        # Exactly one Pi key exists and the turn overwrote it in place.
        assert [k for k in fake.objects if k.endswith(str(pi_blob))] != []
        assert len([k for k in put_keys if k.endswith(str(pi_blob))]) == 1
    async with store.session() as db:
        from apipi.store.repo import list_files

        image_files = await list_files(db, tenant_id)
        assert "image" in [row.filename for row in image_files]
    assert put_headers, "expected presigned PUTs"
    assert all("Authorization" not in headers for headers in put_headers)

    for name, stack in recorded:
        for filename, func in stack:
            assert "/apipi/worker/" not in filename, (name, filename, func)
            assert not filename.endswith("services/sink.py"), (name, filename, func)
            assert not (
                filename.endswith("services/runtime.py") and func == "_harvest_split"
            ), (name, filename, func)

    # A second identical turn uploads nothing new (API presign dedup).
    puts_before = len(put_keys)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    try:
        task2 = asyncio.create_task(
            dispatch_command(execution, _turn_message("again", False))
        )
        await _pump_until_done(
            store,
            outbox,
            waiters,
            worker_id,
            session_id,
            api_settings,
            task2,
            api_objects,
        )
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", real_client)
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None and len(artifacts) == first_count
    # Only the Pi blob re-uploads (same key, as in combined mode); the
    # unchanged workspace file answers `unchanged` with no PUT.
    assert len(put_keys) == puts_before + 1
    assert put_keys[-1].endswith(str(pi_blob))


@pytest.mark.anyio
async def test_split_killed_harvest_without_db(
    store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    """Killed harvest uses remembered identity/env only; worker store is None."""
    from tests.unit.test_blobs import FakeS3

    from apipi.services.event_bus import create_event_bus
    from apipi.services.runtime import FakeHarness
    from apipi.worker.execution import LocalExecution
    from apipi.worker.pi.isolation import load_isolation
    from apipi.worker.pi.pool import PiPool

    api_settings = _s3_settings(tmp_path)
    worker_settings = _s3_settings(tmp_path)
    fake = FakeS3()
    api_objects = S3Store(api_settings, client=fake)

    async def _fake_put(
        url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> None:
        assert fake.presigns, "no presigned URL was issued"
        last = fake.presigns[-1]
        assert isinstance(last.get("Key"), str)
        fake.objects[last["Key"]] = data

    monkeypatch.setattr("apipi.worker.artifact_upload.put_via_url", _fake_put)

    workspace = tmp_path / "killed-ws"
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    (workspace / "outputs" / "partial.txt").write_bytes(b"partial-output")
    tenant_id, session_id, worker_id, _key_id = await _make_session(
        store, directory=str(workspace)
    )
    outbox = Outbox()
    execution = LocalExecution(
        worker_settings,
        pool=PiPool(worker_settings),
        harness=FakeHarness(),
        isolation=load_isolation(worker_settings.run_mode),
        hub=create_event_bus(worker_settings, store=store),
        store=None,
        outbox=outbox,
    )
    assert execution.store is None
    assert execution.blobs is None
    assert execution.objects is None
    # Register tenant identity and remember the hosted directory from a
    # command context, exactly as run_turn/boot do; no database involved.
    execution.sink_for(tenant_id, session_id)
    execution.note_context_ttl(
        session_id,
        {
            "session": {
                "environment": {
                    "type": "openai_hosted",
                    "directory": str(workspace),
                },
                "idle_ttl_seconds": 60,
            }
        },
    )
    stopped: list[uuid.UUID] = []

    async def _note(sid: uuid.UUID) -> None:
        stopped.append(sid)

    execution.note_stopped = _note
    waiters = execution.presign_waiters
    task = asyncio.create_task(execution._harvest_killed(session_id, None))
    await _pump_until_done(
        store,
        outbox,
        waiters,
        worker_id,
        session_id,
        api_settings,
        task,
        api_objects,
    )
    assert stopped == [session_id]
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None
        assert [artifact.path for artifact in artifacts] == ["outputs/partial.txt"]
