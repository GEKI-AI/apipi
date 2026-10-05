from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from tests.support.fake_s3 import FakeS3
from tests.support.http import auth, tenant_of
from tests.support.presign import pump_until_done, s3_settings

from apipi.common.objects import NS_ARTIFACTS
from apipi.services.turn_context import input_image_ref
from apipi.store.blobs import S3Store
from apipi.store.engine import Store
from apipi.store.repo import list_artifacts, list_items
from apipi.worker.outbox import Outbox


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
    from apipi.store.blobs import S3Blobs
    from apipi.store.events import list_events
    from apipi.store.models import utc_now
    from apipi.worker.commands import dispatch_command
    from apipi.worker.execution import local_execution
    from apipi.worker.fake_harness import FakeHarness

    api_settings = s3_settings(tmp_path)
    worker_settings = s3_settings(tmp_path)
    worker_settings.sessions_dir = str(tmp_path / "worker-sessions")
    assert worker_settings.artifact_store == "s3"
    assert not (worker_settings.s3_endpoint or "").strip()

    fake = FakeS3()
    api_objects = S3Store(api_settings, client=fake)
    api_blobs = S3Blobs(api_settings, store=api_objects)
    app = create_app(
        api_settings,
        store=store,
        objects=api_objects,
        blobs=api_blobs,
    )
    worker_id = uuid.uuid4()
    token = "split-turn"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents",
            headers=auth(token),
            json={"name": "bot", "model": "test", "instructions": "Follow."},
        )
        assert agent.status_code == 200
        uploaded = await client.post(
            "/v1/files",
            headers=auth(token),
            data={"purpose": "user_data"},
            files={"file": ("notes.txt", b"workspace notes", "text/plain")},
        )
        assert uploaded.status_code == 200
        file_id = str(uploaded.json()["id"])
        image_upload = await client.post(
            "/v1/files",
            headers=auth(token),
            data={"purpose": "user_data"},
            files={"file": ("photo.png", b"\x89PNG-turn", "image/png")},
        )
        assert image_upload.status_code == 200
        image_id = str(image_upload.json()["id"])
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
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

        tenant_id = tenant_of(token)
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

        def stream(self, method: str, url: str) -> Any:
            assert method == "GET"
            key = urlparse(url).path.lstrip("/")
            data = fake.objects.get(key)
            assert data is not None, f"missing fake S3 key {key}"
            return _FakeResponse(data, 200)

    outbox = Outbox()
    harness = FakeHarness()
    execution = local_execution(worker_settings, harness=harness, outbox=outbox)
    waiters = execution.presign_waiters
    image_ref = input_image_ref(
        api_settings,
        tenant_id,
        image_id,
        mime_type="image/png",
        size_bytes=len(b"\x89PNG-turn"),
        objects=api_objects,
    )
    assert "url" in image_ref and "local_path" not in image_ref

    def _turn_message(text: str, with_image: bool) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
        if with_image:
            parts.append(image_ref)
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

        image_files, _more = await list_files(db, tenant_id)
        assert sorted(row.filename for row in image_files) == [
            "notes.txt",
            "photo.png",
        ]
        user_items = [
            item
            for item in (await list_items(db, tenant_id, session_id)) or []
            if item.data.get("role") == "user"
        ]
    assert user_items[0].data["content"] == [
        {"type": "input_text", "text": "hi"},
        {"type": "input_image", "file_id": image_id},
    ]
    assert harness.images == [
        [
            {
                "type": "image",
                "data": base64.b64encode(b"\x89PNG-turn").decode(),
                "mimeType": "image/png",
            }
        ]
    ]
    assert put_headers, "expected presigned PUTs"
    assert all("Authorization" not in headers for headers in put_headers)

    for name, stack in recorded:
        for filename, func in stack:
            assert "/apipi/worker/" not in filename, (name, filename, func)
            assert not filename.endswith("worker/sink.py"), (name, filename, func)
            assert not (
                filename.endswith("worker/turn_end.py") and func == "harvest_split"
            ), (name, filename, func)

    # A second identical turn uploads nothing new (API presign dedup).
    puts_before = len(put_keys)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    try:
        task2 = asyncio.create_task(
            dispatch_command(execution, _turn_message("again", False))
        )
        await pump_until_done(
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
    # Only the Pi blob re-uploads (same key); the
    # unchanged workspace file answers `unchanged` with no PUT.
    assert len(put_keys) == puts_before + 1
    assert put_keys[-1].endswith(str(pi_blob))
