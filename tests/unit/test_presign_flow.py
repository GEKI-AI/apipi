"""Split-mode credential-less artifact uploads (#448).

The worker holds no object-store credentials and performs no
artifact/file database writes. All uploads go through
`artifact.presign` -> reply -> PUT (S3) or shared-root write
(filesystem) -> `artifact.completed`, for artifact and pi_session
kinds, via the outbox. The legacy input_image kind is refused. Quota
errors surface with today's codes.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from tests.support.config import DATABASE_URL
from tests.support.fake_s3 import FakeS3
from tests.support.presign import flush_outbox, pump_until_done, s3_settings

from apipi.common.objects import NS_ARTIFACTS
from apipi.config import Settings
from apipi.store.blobs import S3Store
from apipi.store.engine import Store
from apipi.store.repo import (
    create_session,
    create_tenant,
    list_artifacts,
    set_session_lease,
)
from apipi.worker.artifact_upload import (
    handle_presign_reply,
    upload_via_presign,
)
from apipi.worker.outbox import Outbox


def _fs_settings(tmp_path: Path, name: str) -> Settings:
    return Settings(
        database_url=DATABASE_URL,
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
    outcome = await flush_outbox(
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
        outcome2 = await flush_outbox(
            store, outbox, session_id, worker_id, api_settings, objects=api_objects
        )
        assert outcome2.rejected == [], outcome2.rejected
    return result


async def _refused_input_image(
    store: Store,
    outbox: Outbox,
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]],
    worker_settings: Settings,
    api_settings: Settings,
    worker_id: uuid.UUID,
    session_id: uuid.UUID,
    api_objects: Any | None = None,
) -> None:
    from sqlalchemy import select

    from apipi.config import DiskLimitError
    from apipi.store.models import ArtifactUploadRow, FileRow

    task = asyncio.create_task(
        upload_via_presign(
            outbox,
            waiters,
            worker_settings,
            session_id,
            kind="input_image",
            filename="image",
            content_type="image/png",
            data=b"\x89PNG-legacy",
        )
    )
    for _ in range(100):
        if outbox.pending(session_id):
            break
        await asyncio.sleep(0.01)
    outcome = await flush_outbox(
        store, outbox, session_id, worker_id, api_settings, objects=api_objects
    )
    assert len(outcome.presign_replies) == 1
    reply = outcome.presign_replies[0]
    assert reply["ok"] is False
    assert reply["code"] == "artifact_store"
    assert "upgrade the worker to 0.15.0" in reply["message"]
    assert not reply.get("url") and not reply.get("path")
    assert not reply.get("upload_id") and not reply.get("file_id")
    assert [reason for _sid, _seq, reason in outcome.rejected] == ["artifact_store"]
    assert outbox.pending(session_id) == []
    assert handle_presign_reply(waiters, reply) is True
    with pytest.raises(DiskLimitError, match="upgrade the worker") as raised:
        await asyncio.wait_for(task, timeout=10)
    assert raised.value.code == "artifact_store"
    assert outbox.pending(session_id) == []
    async with store.session() as db:
        kinds = (await db.scalars(select(ArtifactUploadRow.kind))).all()
        assert "input_image" not in kinds
        assert (await db.scalars(select(FileRow))).all() == []


async def _s3_testbed(store: Store, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """S3 split pair with a counting PUT mock; worker settings hold no keys."""
    api_settings = s3_settings(tmp_path)
    worker_settings = s3_settings(tmp_path)
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


async def _s3_split_flow(store: Store, tmp_path: Path, monkeypatch: Any) -> None:
    bed = await _s3_testbed(store, tmp_path, monkeypatch)
    api_settings = bed["api_settings"]
    worker_settings = bed["worker_settings"]
    fake = bed["fake"]
    api_objects = bed["api_objects"]

    tenant_id, session_id, worker_id, _key_id = await _make_session(store)
    outbox = Outbox()
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}

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
    from apipi.services.turn_context import build_turn_context
    from apipi.worker.turn_context import fetch_pi_session_bytes

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

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def aiter_bytes(self) -> Any:
            yield self.content

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
            return _FakeResponse(await _fake_get(url))

        def stream(self, method: str, url: str) -> Any:
            assert method == "GET"
            return _LazyResponse(url)

    class _LazyResponse(_FakeResponse):
        def __init__(self, url: str) -> None:
            super().__init__(b"")
            self.url = url

        async def __aenter__(self) -> Any:
            self.content = await _fake_get(self.url)
            return self

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    try:
        restored = await fetch_pi_session_bytes(context["pi_session"], worker_settings)
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", real_client)
    assert restored == pi_data

    presigns = len(fake.presigns)
    await _refused_input_image(
        store,
        outbox,
        waiters,
        worker_settings,
        api_settings,
        worker_id,
        session_id,
        api_objects=api_objects,
    )
    assert len(fake.presigns) == presigns


async def _fs_split_flow(store: Store, tmp_path: Path) -> None:
    api_settings = _fs_settings(tmp_path, "api")
    worker_settings = _fs_settings(tmp_path, "worker")
    # Separate sessions dirs, one shared store root.
    assert api_settings.sessions_dir != worker_settings.sessions_dir
    from apipi.common.dirs import store_root

    assert store_root(api_settings) == store_root(worker_settings)

    tenant_id, session_id, worker_id, _key_id = await _make_session(store)
    outbox = Outbox()
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}

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
    from apipi.services.turn_context import build_turn_context
    from apipi.worker.turn_context import fetch_pi_session_bytes

    context = await build_turn_context(store, api_settings, tenant_id, session_id)
    assert context["pi_session"]["present"] is True
    assert context["pi_session"]["local_path"]
    restored = await fetch_pi_session_bytes(context["pi_session"], worker_settings)
    assert restored == pi_data


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
async def test_unchanged_compares_the_newest_artifact_after_a_clock_step_back(
    store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    """A clock step back between two versions keeps the list in creation order."""
    from datetime import datetime, timedelta

    class SteppedBack(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(hours=1)

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
    first = await _upload_roundtrip(**kwargs, data=b"old-bytes")
    monkeypatch.setattr("apipi.store.models.datetime", SteppedBack)
    second = await _upload_roundtrip(**kwargs, data=b"new-bytes")
    assert len(bed["puts"]) == 2
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None
        assert [str(a.id) for a in artifacts] == [
            first["artifact_id"],
            second["artifact_id"],
        ]
    again = await _upload_roundtrip(**kwargs, data=b"old-bytes")
    assert again.get("unchanged") is not True
    assert len(bed["puts"]) == 3
    same = await _upload_roundtrip(**kwargs, data=b"old-bytes")
    assert same.get("unchanged") is True
    assert len(bed["puts"]) == 3
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None
        assert [str(a.id) for a in artifacts] == [
            first["artifact_id"],
            second["artifact_id"],
            again["artifact_id"],
        ]


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


@pytest.mark.anyio
async def test_split_killed_harvest_without_db(
    store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    """Killed harvest uses remembered identity/env only; no worker database."""
    from apipi.services.event_bus import create_event_bus
    from apipi.worker.execution import LocalExecution
    from apipi.worker.fake_harness import FakeHarness
    from apipi.worker.pi.pool import PiPool

    bed = await _s3_testbed(store, tmp_path, monkeypatch)
    api_settings = bed["api_settings"]
    worker_settings = bed["worker_settings"]
    api_objects = bed["api_objects"]

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
        hub=create_event_bus(worker_settings, store=store),
        outbox=outbox,
    )
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
    await pump_until_done(
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
