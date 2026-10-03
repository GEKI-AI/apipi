"""Artifacts via API-issued presigned PUT (#448)."""

import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.test_blobs import FakeS3

from apipi.config import ConfigError, Settings
from apipi.protocol import (
    MAX_MESSAGE_BYTES,
    PAYLOAD_MODELS,
    parse_envelope,
)
from apipi.services.ingest import IngestBatcher, flush_batch
from apipi.services.worker_artifacts import (
    SHARED_STORE_ERROR,
    check_completed_path,
    read_store_check,
    sha256_hex,
    write_store_check,
)
from apipi.store.blobs import (
    NS_ARTIFACTS,
    LocalStore,
    S3Store,
    blob_key,
)
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    list_artifacts,
    set_session_lease,
)
from apipi.worker.artifact_upload import (
    completed_envelope,
    handle_presign_reply,
    presign_envelope,
    write_shared_object,
)
from apipi.worker.hub import answer_store_check
from apipi.worker.pi.dirs import store_root


class _ListableFakeS3(FakeS3):
    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        prefix = str(kwargs.get("Prefix") or "")
        contents = [
            {"Key": key, "Size": len(data)}
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


def _settings(tmp_path: Path, **overrides) -> Settings:
    kwargs: dict = {
        "database_url": "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        "run_mode": "none",
        "sessions_dir": str(tmp_path / "sessions"),
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


async def _leased(store: Store, worker_id: uuid.UUID):
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}
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
        return tenant.id, row.id, lease_id, row.key_id


def test_local_store_dir_defaults_to_apipi_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APIPI_LOCAL_STORE_DIR", raising=False)
    settings = _settings(tmp_path)
    assert settings.local_store_dir == ".apipi/store"
    assert store_root(settings) == Path(".apipi/store")
    assert store_root(settings) != Path(settings.sessions_dir or "")
    custom = _settings(tmp_path, local_store_dir=str(tmp_path / "shared"))
    assert store_root(custom) == tmp_path / "shared"
    assert LocalStore(custom)._root == tmp_path / "shared"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_local_store_dir_blank_is_rejected(tmp_path: Path, blank: str | None) -> None:
    with pytest.raises(Exception, match="APIPI_LOCAL_STORE_DIR"):
        _settings(tmp_path, local_store_dir=blank)
    with pytest.raises(Exception, match="APIPI_LOCAL_STORE_DIR"):
        _settings(tmp_path, api_url="http://api.example:8000", local_store_dir=blank)


def test_blank_local_store_dir_is_fine_for_s3(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, artifact_store="s3", s3_bucket="bucket", local_store_dir=""
    )
    assert settings.artifact_store == "s3"


def test_no_message_schema_carries_bytes() -> None:

    for name, model in PAYLOAD_MODELS.items():
        for field_name, field in model.model_fields.items():
            annotation = str(field.annotation)
            assert "bytes" not in annotation.lower(), (name, field_name)
            assert field_name not in {"data_bytes", "content", "bytes"}, (
                name,
                field_name,
            )
    assert MAX_MESSAGE_BYTES == 1_000_000


def test_presign_envelope_has_no_bytes() -> None:
    session_id = uuid.uuid4()
    _request_id, payload = presign_envelope(
        session_id,
        kind="artifact",
        filename="a.txt",
        content_type="text/plain",
        data=b"hello",
    )
    assert payload["size"] == 5
    assert isinstance(payload["sha256"], str)
    envelope = parse_envelope(
        {
            "v": 2,
            "session_id": str(session_id),
            "seq": 1,
            "type": "artifact.presign",
            "payload": payload,
        }
    )
    assert envelope.message_class() == "durable"


def test_shared_store_check_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    marker, nonce = write_store_check(root)
    assert read_store_check(root, marker, nonce) is True
    assert read_store_check(tmp_path / "other", marker, nonce) is False
    assert read_store_check(root, marker, "wrong") is False
    assert read_store_check(root, "../evil", nonce) is False


def test_answer_store_check_rejects_without_shared_root(tmp_path: Path) -> None:
    api_settings = _settings(tmp_path, local_store_dir=str(tmp_path / "shared"))
    worker_settings = _settings(tmp_path, local_store_dir=str(tmp_path / "elsewhere"))
    from apipi.services.worker_artifacts import write_store_check as _write

    root = store_root(api_settings)
    marker, nonce = _write(root)
    hello = {"store_check": {"marker": marker, "nonce": nonce}}
    with pytest.raises(ConfigError, match="shared path"):
        answer_store_check(worker_settings, hello)
    proof = answer_store_check(api_settings, hello)
    assert proof is not None and proof["marker"] == marker
    assert answer_store_check(api_settings, {}) is None


def test_check_completed_path_rejects_traversal() -> None:
    from apipi.store.blobs import ObjectStoreError

    assert check_completed_path("a/b.txt") == "a/b.txt"
    for bad in ["", "../evil", "a/../../b", "/abs", "a//b", "a/./b"]:
        with pytest.raises(ObjectStoreError):
            check_completed_path(bad)


async def _flush(store, worker_id, envelopes, settings, objects=None):
    batcher = IngestBatcher()
    for envelope in envelopes:
        batcher.add(envelope, 128)
    return await flush_batch(
        store,
        batcher.take(),
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        objects=objects,
    )


def _presign_payload(request_id: uuid.UUID, size: int = 5) -> dict:
    return {
        "request_id": str(request_id),
        "kind": "artifact",
        "filename": "out.txt",
        "content_type": "text/plain",
        "size": size,
        "sha256": sha256_hex(b"hello"),
    }


def _envelope(session_id, seq, type, payload):
    from apipi.protocol import WorkerEnvelope

    return WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "seq": seq,
            "type": type,
            "payload": payload,
        }
    )


async def test_quota_before_presign(store: Store, tmp_path: Path) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease, _key = await _leased(store, worker_id)
    settings = _settings(
        tmp_path, max_artifact_bytes=10, local_store_dir=str(tmp_path / "s")
    )
    request_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id, 1, "artifact.presign", _presign_payload(request_id, 64)
            )
        ],
        settings,
    )
    assert outcome.acks == {session_id: 1}
    assert len(outcome.presign_replies) == 1
    reply = outcome.presign_replies[0]
    assert reply["ok"] is False
    assert reply["code"] in {
        "artifact_too_large",
        "workspace_too_large",
        "artifact_store",
    }
    assert [reason for _, _, reason in outcome.rejected] == [reply["code"]]


async def test_filesystem_presign_then_completed(store: Store, tmp_path: Path) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease, key_id = await _leased(store, worker_id)
    shared = tmp_path / "shared"
    settings = _settings(tmp_path, local_store_dir=str(shared))
    data = b"artifact-bytes"
    request_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "artifact.presign",
                {
                    "request_id": str(request_id),
                    "kind": "artifact",
                    "filename": "out.txt",
                    "content_type": "text/plain",
                    "size": len(data),
                    "sha256": sha256_hex(data),
                },
            )
        ],
        settings,
    )
    assert outcome.presign_replies[0]["ok"] is True
    upload_id = uuid.UUID(outcome.presign_replies[0]["upload_id"])
    async with store.session() as db:
        from apipi.store.repo import get_artifact_upload

        upload = await get_artifact_upload(db, tenant_id, upload_id)
        assert upload is not None
        object_id = blob_key(tenant_id, key_id, session_id, upload.artifact_id)
    relative = write_shared_object(settings, object_id, data)
    outcome2 = await _flush(
        store,
        worker_id,
        [
            _envelope(
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
        ],
        settings,
    )
    assert outcome2.rejected == []
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None
        assert len(artifacts) == 1
        assert artifacts[0].byte_size == len(data)


async def test_checksum_mismatch_rejected(store: Store, tmp_path: Path) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease, key_id = await _leased(store, worker_id)
    settings = _settings(tmp_path, local_store_dir=str(tmp_path / "shared"))
    data = b"real-bytes"
    request_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "artifact.presign",
                _presign_payload(request_id, len(data)),
            )
        ],
        settings,
    )
    upload_id = uuid.UUID(outcome.presign_replies[0]["upload_id"])
    async with store.session() as db:
        from apipi.store.repo import get_artifact_upload

        upload = await get_artifact_upload(db, tenant_id, upload_id)
        assert upload is not None
        object_id = blob_key(tenant_id, key_id, session_id, upload.artifact_id)
    relative = write_shared_object(settings, object_id, data)
    outcome2 = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                2,
                "artifact.completed",
                {
                    "upload_id": str(upload_id),
                    "path": relative,
                    "size": len(data),
                    "sha256": "0" * 64,
                },
            )
        ],
        settings,
    )
    assert len(outcome2.rejected) == 1


async def test_foreign_upload_id_rejected(store: Store, tmp_path: Path) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease, _key = await _leased(store, worker_id)
    _t2, session2, _l2, _k2 = await _leased(store, worker_id)
    settings = _settings(tmp_path, local_store_dir=str(tmp_path / "shared"))
    request_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [_envelope(session2, 1, "artifact.presign", _presign_payload(request_id))],
        settings,
    )
    upload_id = outcome.presign_replies[0]["upload_id"]
    outcome2 = await _flush(
        store,
        worker_id,
        [_envelope(session_id, 2, "artifact.completed", {"upload_id": upload_id})],
        settings,
    )
    assert len(outcome2.rejected) == 1


async def test_path_traversal_in_completed_rejected(
    store: Store, tmp_path: Path
) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease, _key = await _leased(store, worker_id)
    settings = _settings(tmp_path, local_store_dir=str(tmp_path / "shared"))
    request_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [_envelope(session_id, 1, "artifact.presign", _presign_payload(request_id))],
        settings,
    )
    upload_id = outcome.presign_replies[0]["upload_id"]
    outcome2 = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                2,
                "artifact.completed",
                {"upload_id": upload_id, "path": "../evil.txt"},
            )
        ],
        settings,
    )
    assert len(outcome2.rejected) == 1


async def test_s3_presign_bound_to_session_prefix(store: Store, tmp_path: Path) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease, key_id = await _leased(store, worker_id)
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_endpoint="https://s3.example",
        s3_region="us-east-1",
    )
    fake = _ListableFakeS3()
    objects = S3Store(settings, client=fake)
    data = b"s3-bytes"
    request_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "artifact.presign",
                {
                    "request_id": str(request_id),
                    "kind": "artifact",
                    "filename": "out.bin",
                    "content_type": "application/octet-stream",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
        ],
        settings,
        objects=objects,
    )
    reply = outcome.presign_replies[0]
    assert reply["ok"] is True
    assert reply["url"] is not None and "presign" in reply["url"]
    assert fake.presigns, "presigned PUT URL was not issued"
    key = fake.presigns[0]["Key"]
    assert str(tenant_id) in key and str(session_id) in key
    assert reply["upload_id"]
    async with store.session() as db:
        from apipi.store.repo import get_artifact_upload

        upload = await get_artifact_upload(db, tenant_id, uuid.UUID(reply["upload_id"]))
        assert upload is not None
        object_id = blob_key(tenant_id, key_id, session_id, upload.artifact_id)
    from apipi.store.blobs import s3_object_key

    assert key == s3_object_key(settings, NS_ARTIFACTS, object_id)
    # Worker uploads with no credentials, just the URL.
    objects_sync = S3Store(settings, client=fake)
    await objects_sync.put(NS_ARTIFACTS, object_id, data)
    outcome2 = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                2,
                "artifact.completed",
                {
                    "upload_id": reply["upload_id"],
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "name": "out.bin",
                },
            )
        ],
        settings,
        objects=objects,
    )
    assert outcome2.rejected == []
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None
        assert len(artifacts) == 1


async def test_s3_presign_ttl_short(store: Store, tmp_path: Path) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease, _key = await _leased(store, worker_id)
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_endpoint="https://s3.example",
        s3_region="us-east-1",
        presign_ttl="5m",
    )
    assert settings.presign_ttl <= timedelta(minutes=15)
    fake = _ListableFakeS3()
    outcome = await _flush(
        store,
        worker_id,
        [_envelope(session_id, 1, "artifact.presign", _presign_payload(uuid.uuid4()))],
        settings,
        objects=S3Store(settings, client=fake),
    )
    assert outcome.presign_replies[0]["ok"] is True
    assert outcome.presign_replies[0]["expires_at"]


def test_completed_envelope_helper_has_no_bytes() -> None:
    payload = completed_envelope(
        uuid.uuid4(),
        upload_id=uuid.UUID(int=0),
        data=b"abc",
        path="a/b",
    )
    assert payload["size"] == 3
    assert b"abc" not in str(payload).encode()


def test_handle_presign_reply() -> None:
    import asyncio

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
        request_id = uuid.uuid4()
        waiters = {request_id: future}
        assert (
            handle_presign_reply(
                waiters,
                {
                    "type": "artifact.presign.reply",
                    "request_id": str(request_id),
                    "ok": True,
                },
            )
            is True
        )
        assert (await future)["ok"] is True
        assert handle_presign_reply(waiters, {"type": "other"}) is False

    asyncio.run(_run())


def test_error_message_mentions_shared_path() -> None:
    assert "APIPI_LOCAL_STORE_DIR" in SHARED_STORE_ERROR
